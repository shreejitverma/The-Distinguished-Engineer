#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#if defined(__aarch64__) || defined(__ARM_NEON)
#include <arm_neon.h>
#define ARCH_NEON 1
#define MEMCHR_PATH "NEON (vceqq_u8, vmaxvq_u8)"
#define TEDDY_PATH "NEON (vqtbl1q_u8)"
#elif defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#include <tmmintrin.h>
#define ARCH_X86 1
#define MEMCHR_PATH "SSE2 (_mm_cmpeq_epi8, _mm_movemask_epi8)"
#define TEDDY_PATH "SSSE3 (_mm_shuffle_epi8)"
#else
#define ARCH_GENERIC 1
#define MEMCHR_PATH "scalar fallback"
#define TEDDY_PATH "scalar fallback"
#endif

/*
 * simd_search.c - Reference implementation of ripgrep text search mechanics:
 * 1. Vectorized single-byte literal prefilter (memchr model)
 * 2. Vectorized multi-pattern packed search (Slim Teddy 8-pattern model)
 * 3. Rolling buffer stream scanner (grep-searcher model)
 * 4. Byte-oriented bstr scanning avoiding UTF-8 validation overhead
 */

/* ========================================================================= */
/* 1. Single-byte SIMD literal search (memchr)                               */
/* ========================================================================= */

typedef struct {
    size_t total_bytes;
    size_t vector_chunks;
    size_t scalar_tail_bytes;
    size_t match_count;
} MemchrStats;

MemchrStats run_simd_memchr(const uint8_t *haystack, size_t len, uint8_t needle) {
    MemchrStats stats = {len, 0, 0, 0};
    size_t i = 0;

#if defined(ARCH_NEON)
    uint8x16_t target = vdupq_n_u8(needle);
    for (; i + 16 <= len; i += 16) {
        stats.vector_chunks++;
        uint8x16_t chunk = vld1q_u8(haystack + i);
        uint8x16_t cmp = vceqq_u8(chunk, target);
        if (vmaxvq_u8(cmp) != 0) {
            uint8_t matches[16];
            vst1q_u8(matches, cmp);
            for (size_t j = 0; j < 16; j++) {
                if (matches[j]) {
                    stats.match_count++;
                }
            }
        }
    }
#elif defined(ARCH_X86)
    __m128i target = _mm_set1_epi8((char)needle);
    for (; i + 16 <= len; i += 16) {
        stats.vector_chunks++;
        __m128i chunk = _mm_loadu_si128((const __m128i*)(haystack + i));
        __m128i cmp = _mm_cmpeq_epi8(chunk, target);
        int mask = _mm_movemask_epi8(cmp);
        if (mask != 0) {
            stats.match_count += (size_t)__builtin_popcount(mask);
        }
    }
#else
    for (; i + 16 <= len; i += 16) {
        stats.vector_chunks++;
        for (size_t j = 0; j < 16; j++) {
            if (haystack[i + j] == needle) {
                stats.match_count++;
            }
        }
    }
#endif

    /* Scalar tail */
    for (; i < len; i++) {
        stats.scalar_tail_bytes++;
        if (haystack[i] == needle) {
            stats.match_count++;
        }
    }

    return stats;
}

/* ========================================================================= */
/* 2. Vectorized Multi-Pattern Packed Search (Slim Teddy 8-Pattern)          */
/* ========================================================================= */

#define MAX_PATTERNS 8

typedef struct {
    const char *str;
    size_t len;
} Pattern;

typedef struct {
    uint8_t mask_lo[16];
    uint8_t mask_hi[16];
    Pattern patterns[MAX_PATTERNS];
    size_t pattern_count;
} TeddyMatcher;

void teddy_compile(TeddyMatcher *m, const char *patterns[], size_t count) {
    memset(m->mask_lo, 0, sizeof(m->mask_lo));
    memset(m->mask_hi, 0, sizeof(m->mask_hi));
    m->pattern_count = count < MAX_PATTERNS ? count : MAX_PATTERNS;

    for (size_t k = 0; k < m->pattern_count; k++) {
        m->patterns[k].str = patterns[k];
        m->patterns[k].len = strlen(patterns[k]);
        if (m->patterns[k].len == 0) continue;

        uint8_t first_byte = (uint8_t)patterns[k][0];
        uint8_t lo = first_byte & 0x0F;
        uint8_t hi = (first_byte >> 4) & 0x0F;

        m->mask_lo[lo] |= (uint8_t)(1 << k);
        m->mask_hi[hi] |= (uint8_t)(1 << k);
    }
}

typedef struct {
    size_t total_bytes;
    size_t total_chunks;
    size_t chunks_rejected;
    size_t candidates_checked;
    size_t matches_confirmed;
} TeddyStats;

TeddyStats teddy_search(const TeddyMatcher *m, const uint8_t *haystack, size_t len) {
    TeddyStats stats = {len, 0, 0, 0, 0};
    size_t i = 0;

#if defined(ARCH_NEON)
    uint8x16_t t_lo = vld1q_u8(m->mask_lo);
    uint8x16_t t_hi = vld1q_u8(m->mask_hi);
    uint8x16_t lo_mask = vdupq_n_u8(0x0F);
#elif defined(ARCH_X86)
    __m128i t_lo = _mm_loadu_si128((const __m128i*)m->mask_lo);
    __m128i t_hi = _mm_loadu_si128((const __m128i*)m->mask_hi);
    __m128i lo_mask = _mm_set1_epi8(0x0F);
#endif

    for (; i + 16 <= len; i += 16) {
        stats.total_chunks++;
        uint8_t candidate_masks[16];
        bool has_candidate = false;

#if defined(ARCH_NEON)
        uint8x16_t chunk = vld1q_u8(haystack + i);
        uint8x16_t lo_nibbles = vandq_u8(chunk, lo_mask);
        uint8x16_t hi_nibbles = vshrq_n_u8(chunk, 4);

        uint8x16_t res_lo = vqtbl1q_u8(t_lo, lo_nibbles);
        uint8x16_t res_hi = vqtbl1q_u8(t_hi, hi_nibbles);
        uint8x16_t res = vandq_u8(res_lo, res_hi);

        if (vmaxvq_u8(res) == 0) {
            stats.chunks_rejected++;
            continue;
        }
        vst1q_u8(candidate_masks, res);
        has_candidate = true;
#elif defined(ARCH_X86)
        __m128i chunk = _mm_loadu_si128((const __m128i*)(haystack + i));
        __m128i lo_nibbles = _mm_and_si128(chunk, lo_mask);
        __m128i hi_nibbles = _mm_and_si128(_mm_srli_epi16(chunk, 4), lo_mask);

        __m128i res_lo = _mm_shuffle_epi8(t_lo, lo_nibbles);
        __m128i res_hi = _mm_shuffle_epi8(t_hi, hi_nibbles);
        __m128i res = _mm_and_si128(res_lo, res_hi);

        int mask = _mm_movemask_epi8(_mm_cmpeq_epi8(res, _mm_setzero_si128()));
        if (mask == 0xFFFF) {
            stats.chunks_rejected++;
            continue;
        }
        _mm_storeu_si128((__m128i*)candidate_masks, res);
        has_candidate = true;
#else
        for (size_t j = 0; j < 16; j++) {
            uint8_t byte = haystack[i + j];
            uint8_t lo = byte & 0x0F;
            uint8_t hi = (byte >> 4) & 0x0F;
            candidate_masks[j] = m->mask_lo[lo] & m->mask_hi[hi];
            if (candidate_masks[j] != 0) {
                has_candidate = true;
            }
        }
        if (!has_candidate) {
            stats.chunks_rejected++;
            continue;
        }
#endif

        if (has_candidate) {
            /* Verify candidates */
            for (size_t j = 0; j < 16; j++) {
                uint8_t cmask = candidate_masks[j];
                if (cmask == 0) continue;

                size_t offset = i + j;
                for (size_t k = 0; k < m->pattern_count; k++) {
                    if (cmask & (1 << k)) {
                        stats.candidates_checked++;
                        size_t pat_len = m->patterns[k].len;
                        if (offset + pat_len <= len) {
                            if (memcmp(haystack + offset, m->patterns[k].str, pat_len) == 0) {
                                stats.matches_confirmed++;
                            }
                        }
                    }
                }
            }
        }
    }

    /* Scalar remainder */
    for (; i < len; i++) {
        uint8_t byte = haystack[i];
        uint8_t lo = byte & 0x0F;
        uint8_t hi = (byte >> 4) & 0x0F;
        uint8_t cmask = m->mask_lo[lo] & m->mask_hi[hi];
        if (cmask == 0) continue;

        for (size_t k = 0; k < m->pattern_count; k++) {
            if (cmask & (1 << k)) {
                stats.candidates_checked++;
                size_t pat_len = m->patterns[k].len;
                if (i + pat_len <= len) {
                    if (memcmp(haystack + i, m->patterns[k].str, pat_len) == 0) {
                        stats.matches_confirmed++;
                    }
                }
            }
        }
    }

    return stats;
}

/* ========================================================================= */
/* 3. Rolling Buffer Stream Scanner (grep-searcher simulation)               */
/* ========================================================================= */

typedef struct {
    size_t line_number;
    size_t byte_offset;
    char line_snippet[64];
} MatchResult;

size_t search_stream_rolling_buffer(
    const uint8_t *stream,
    size_t stream_len,
    size_t chunk_size,
    const char *needle,
    MatchResult *results,
    size_t max_results
) {
    size_t matches_found = 0;
    size_t needle_len = strlen(needle);
    uint8_t *buffer = (uint8_t *)malloc(chunk_size * 2);
    if (!buffer) return 0;

    size_t stream_pos = 0;
    size_t buf_len = 0;
    size_t global_line = 1;
    size_t global_byte_offset = 0;

    while (stream_pos < stream_len || buf_len > 0) {
        /* Fill buffer up to chunk_size */
        if (stream_pos < stream_len && buf_len < chunk_size) {
            size_t to_read = chunk_size - buf_len;
            if (stream_pos + to_read > stream_len) {
                to_read = stream_len - stream_pos;
            }
            memcpy(buffer + buf_len, stream + stream_pos, to_read);
            buf_len += to_read;
            stream_pos += to_read;
        }

        /* Find last newline in current buffer to know safe search boundary */
        int last_nl = -1;
        for (ssize_t idx = (ssize_t)buf_len - 1; idx >= 0; idx--) {
            if (buffer[idx] == '\n') {
                last_nl = (int)idx;
                break;
            }
        }

        /* If stream is exhausted, search entire remaining buffer */
        size_t search_bound = (stream_pos >= stream_len) ? buf_len : (last_nl >= 0 ? (size_t)(last_nl + 1) : 0);

        if (search_bound == 0 && buf_len >= chunk_size) {
            /* Line is longer than chunk_size; search what we have */
            search_bound = buf_len;
        }

        /* Search lines up to search_bound */
        size_t line_start = 0;
        for (size_t idx = 0; idx < search_bound; idx++) {
            if (buffer[idx] == '\n' || idx + 1 == search_bound) {
                size_t line_end = (buffer[idx] == '\n') ? idx : idx + 1;
                size_t line_bytes = line_end - line_start;

                /* Search needle in this line */
                if (line_bytes >= needle_len) {
                    for (size_t s = line_start; s + needle_len <= line_end; s++) {
                        if (memcmp(buffer + s, needle, needle_len) == 0) {
                            if (matches_found < max_results) {
                                results[matches_found].line_number = global_line;
                                results[matches_found].byte_offset = global_byte_offset + s;
                                size_t snip_len = line_bytes < 63 ? line_bytes : 63;
                                memcpy(results[matches_found].line_snippet, buffer + line_start, snip_len);
                                results[matches_found].line_snippet[snip_len] = '\0';
                                matches_found++;
                            }
                            break;
                        }
                    }
                }

                if (buffer[idx] == '\n') {
                    global_line++;
                }
                line_start = idx + 1;
            }
        }

        /* Roll remainder to buffer front */
        if (search_bound < buf_len) {
            size_t remainder = buf_len - search_bound;
            memmove(buffer, buffer + search_bound, remainder);
            buf_len = remainder;
            global_byte_offset += search_bound;
        } else {
            global_byte_offset += buf_len;
            buf_len = 0;
        }

        if (stream_pos >= stream_len && buf_len == 0) {
            break;
        }
    }

    free(buffer);
    return matches_found;
}

/* ========================================================================= */
/* 4. Byte-oriented bstr scanning vs UTF-8 Validation                        */
/* ========================================================================= */

bool is_valid_utf8(const uint8_t *bytes, size_t len) {
    size_t i = 0;
    while (i < len) {
        uint8_t c = bytes[i];
        if (c <= 0x7F) {
            i++;
        } else if ((c & 0xE0) == 0xC0) {
            if (i + 1 >= len) return false;
            if ((bytes[i + 1] & 0xC0) != 0x80) return false;
            if (c < 0xC2) return false; /* Overlong */
            i += 2;
        } else if ((c & 0xF0) == 0xE0) {
            if (i + 2 >= len) return false;
            if ((bytes[i + 1] & 0xC0) != 0x80) return false;
            if ((bytes[i + 2] & 0xC0) != 0x80) return false;
            i += 3;
        } else if ((c & 0xF8) == 0xF0) {
            if (i + 3 >= len) return false;
            if ((bytes[i + 1] & 0xC0) != 0x80) return false;
            if ((bytes[i + 2] & 0xC0) != 0x80) return false;
            if ((bytes[i + 3] & 0xC0) != 0x80) return false;
            i += 4;
        } else {
            return false;
        }
    }
    return true;
}

/* ========================================================================= */
/* Independent oracles and check bookkeeping                                 */
/* ========================================================================= */

static int check_failures = 0;
static int check_count = 0;

#define CHECK(cond, msg) \
    do { \
        check_count++; \
        if (!(cond)) { \
            check_failures++; \
            fprintf(stderr, "CHECK FAILED: %s (line %d)\n", (msg), __LINE__); \
        } \
    } while (0)

/* Plain byte loop: shares no code with the vector paths above. */
static size_t ref_count_byte(const uint8_t *hay, size_t len, uint8_t needle) {
    size_t n = 0;
    for (size_t i = 0; i < len; i++) {
        if (hay[i] == needle) n++;
    }
    return n;
}

/* Offset of the first occurrence of needle in hay[0..len), or -1. */
static long ref_find(const uint8_t *hay, size_t len, const char *needle) {
    size_t nl = strlen(needle);
    for (size_t i = 0; i + nl <= len; i++) {
        if (memcmp(hay + i, needle, nl) == 0) return (long)i;
    }
    return -1;
}

/* ========================================================================= */
/* Main test harness                                                         */
/* ========================================================================= */

int main(void) {
    printf("=== Ripgrep Text Search Mechanics Test Suite ===\n\n");

    /* --------------------------------------------------------------------- */
    /* Test 1: Single-byte Vectorized memchr                                 */
    /* --------------------------------------------------------------------- */
    printf("--- Phase 1: Vectorized memchr Prefilter ---\n");
    static uint8_t corpus1[512];
    for (size_t i = 0; i < sizeof(corpus1); i++) {
        corpus1[i] = (uint8_t)('a' + (i % 26));
    }
    /* Place target newline characters at specific positions */
    corpus1[63] = '\n';
    corpus1[127] = '\n';
    corpus1[191] = '\n';
    corpus1[255] = '\n';
    corpus1[319] = '\n';
    corpus1[383] = '\n';
    corpus1[447] = '\n';
    corpus1[511] = '\n';

    MemchrStats mstats = run_simd_memchr(corpus1, sizeof(corpus1), '\n');
    printf("memchr SIMD path: %s\n", MEMCHR_PATH);
    printf("memchr total bytes: %zu\n", mstats.total_bytes);
    printf("memchr 16B vector chunks: %zu\n", mstats.vector_chunks);
    printf("memchr scalar tail bytes: %zu\n", mstats.scalar_tail_bytes);
    printf("memchr match count: %zu\n", mstats.match_count);

    CHECK(mstats.total_bytes == 512, "memchr: total bytes");
    CHECK(mstats.vector_chunks == 32, "memchr: 32 vector chunks");
    CHECK(mstats.scalar_tail_bytes == 0, "memchr: no scalar tail");
    CHECK(mstats.match_count == ref_count_byte(corpus1, sizeof(corpus1), '\n'), "memchr: count equals byte-loop oracle");
    CHECK(mstats.match_count == 8, "memchr: 8 newlines");
    /* Sweep every length around the 16-byte lane width with a needle every 7th byte, plus an
     * all-needle buffer: the vector/tail split and the per-lane count must agree with the oracle. */
    for (size_t len = 0; len <= 80; len++) {
        uint8_t buf[80];
        for (size_t i = 0; i < 80; i++) buf[i] = (i % 7 == 3) ? 'Q' : 'z';
        MemchrStats a = run_simd_memchr(buf, len, 'Q');
        CHECK(a.match_count == ref_count_byte(buf, len, 'Q'), "memchr sweep: count equals oracle");
        CHECK(a.vector_chunks == len / 16 && a.scalar_tail_bytes == len % 16, "memchr sweep: chunk/tail split");
        memset(buf, 'Q', sizeof(buf));
        MemchrStats b = run_simd_memchr(buf, len, 'Q');
        CHECK(b.match_count == len, "memchr sweep: all lanes match");
    }

    /* --------------------------------------------------------------------- */
    /* Test 2: Slim Teddy (8 patterns) Prefilter                             */
    /* --------------------------------------------------------------------- */
    printf("\n--- Phase 2: Slim Teddy 8-Pattern Vector Filter ---\n");
    const char *teddy_patterns[8] = {
        "alpha", "bravo", "charlie", "delta",
        "echo", "foxtrot", "golf", "hotel"
    };

    TeddyMatcher matcher;
    teddy_compile(&matcher, teddy_patterns, 8);

    /* Print compiled Teddy nibble masks for verification */
    printf("Teddy SIMD path: %s\n", TEDDY_PATH);
    printf("Teddy pattern count: %zu\n", matcher.pattern_count);
    printf("Teddy mask_lo non-zero buckets:");
    for (int n = 0; n < 16; n++) {
        if (matcher.mask_lo[n]) {
            printf(" [%d]=0x%02x", n, matcher.mask_lo[n]);
        }
    }
    printf("\n");

    printf("Teddy mask_hi non-zero buckets:");
    for (int n = 0; n < 16; n++) {
        if (matcher.mask_hi[n]) {
            printf(" [%d]=0x%02x", n, matcher.mask_hi[n]);
        }
    }
    printf("\n");

    /* Create test corpus with background text and embedded patterns */
    static uint8_t corpus2[1024];
    memset(corpus2, '.', sizeof(corpus2));
    /* Embed patterns at specific offsets */
    memcpy(corpus2 + 48, "alpha", 5);
    memcpy(corpus2 + 160, "charlie", 7);
    memcpy(corpus2 + 352, "foxtrot", 7);
    memcpy(corpus2 + 704, "hotel", 5);

    TeddyStats tstats = teddy_search(&matcher, corpus2, sizeof(corpus2));
    size_t rejection_rate = (tstats.chunks_rejected * 100) / tstats.total_chunks;
    printf("Teddy total bytes: %zu\n", tstats.total_bytes);
    printf("Teddy total chunks (16B): %zu\n", tstats.total_chunks);
    printf("Teddy chunks rejected early: %zu\n", tstats.chunks_rejected);
    printf("Teddy early rejection rate: %zu%%\n", rejection_rate);
    printf("Teddy candidates checked: %zu\n", tstats.candidates_checked);
    printf("Teddy matches confirmed: %zu\n", tstats.matches_confirmed);

    /* Oracle: every position whose byte equals some pattern's first byte is a candidate; every
     * position where a whole pattern is present is a confirmed match; a chunk with no candidate
     * position is rejected. None of this uses the nibble tables. */
    size_t o_cand = 0, o_match = 0, o_rejected = 0;
    for (size_t c = 0; c + 16 <= sizeof(corpus2); c += 16) {
        bool any = false;
        for (size_t j = c; j < c + 16; j++) {
            for (size_t k = 0; k < 8; k++) {
                if (corpus2[j] == (uint8_t)teddy_patterns[k][0]) { o_cand++; any = true; }
            }
        }
        if (!any) o_rejected++;
    }
    for (size_t i = 0; i < sizeof(corpus2); i++) {
        for (size_t k = 0; k < 8; k++) {
            size_t pl = strlen(teddy_patterns[k]);
            if (i + pl <= sizeof(corpus2) && memcmp(corpus2 + i, teddy_patterns[k], pl) == 0) o_match++;
        }
    }
    CHECK(tstats.total_chunks == 64, "teddy: 64 chunks");
    CHECK(tstats.candidates_checked == o_cand, "teddy: candidates equal first-byte oracle");
    CHECK(tstats.matches_confirmed == o_match, "teddy: matches equal memcmp oracle");
    CHECK(tstats.chunks_rejected == o_rejected, "teddy: rejected chunks equal oracle");
    CHECK(tstats.matches_confirmed == 4, "teddy: 4 embedded patterns");
    CHECK(tstats.chunks_rejected + 4 <= tstats.total_chunks && tstats.chunks_rejected == 60, "teddy: 60 rejected chunks");
    /* Soundness: a pattern placed at every offset 0..15 of a chunk must always be found,
     * including when it straddles into the next chunk. */
    for (size_t off = 0; off < 32; off++) {
        for (size_t k = 0; k < 8; k++) {
            static uint8_t t[96];
            memset(t, '.', sizeof(t));
            memcpy(t + off, teddy_patterns[k], strlen(teddy_patterns[k]));
            TeddyStats ts = teddy_search(&matcher, t, sizeof(t));
            CHECK(ts.matches_confirmed == 1, "teddy sweep: pattern found at every offset");
        }
    }

    /* --------------------------------------------------------------------- */
    /* Test 3: Rolling Buffer Boundary Refill                                */
    /* --------------------------------------------------------------------- */
    printf("\n--- Phase 3: Rolling Buffer Stream Refill ---\n");
    /* Construct a stream where the needle itself straddles the 64-byte chunk boundary (stream offset 64) */
    char stream[256];
    memset(stream, ' ', sizeof(stream));
    /* Line 1: 50 bytes ending with \n */
    const char *line1 = "Line 01: Standard prefix line filler content here.\n";
    memcpy(stream, line1, strlen(line1));
    /* Line 2: starts at offset 51; the needle begins at stream offset 60 and ends at 73, across offset 64 */
    const char *line2 = "Line 02: needle_target straddles the 64-byte chunk boundary.\n";
    memcpy(stream + strlen(line1), line2, strlen(line2));
    /* Line 3 */
    const char *line3 = "Line 03: Trailing block.\n";
    memcpy(stream + strlen(line1) + strlen(line2), line3, strlen(line3));
    size_t stream_len = strlen(line1) + strlen(line2) + strlen(line3);

    MatchResult results[4];
    size_t found = search_stream_rolling_buffer(
        (const uint8_t *)stream,
        stream_len,
        64, /* 64-byte chunk size deliberately forcing line2 to straddle boundary */
        "needle_target",
        results,
        4
    );

    const long needle_off = ref_find((const uint8_t *)stream, stream_len, "needle_target");
    size_t oracle_line = 1;
    for (long q = 0; q < needle_off; q++) {
        if (stream[q] == '\n') oracle_line++;
    }
    CHECK(needle_off >= 0 && needle_off < 64 && needle_off + 13 > 64, "stream: needle straddles stream offset 64");
    CHECK(ref_find((const uint8_t *)stream, 64, "needle_target") < 0, "stream: first 64-byte chunk alone lacks the whole needle");
    CHECK(ref_find((const uint8_t *)stream + 64, stream_len - 64, "needle_target") < 0, "stream: second chunk alone lacks the whole needle");

    /* Naive scanner: each fixed 64-byte chunk searched on its own, no carry-over. It must miss the needle. */
    size_t naive_found = 0;
    for (size_t c = 0; c < stream_len; c += 64) {
        size_t n = stream_len - c < 64 ? stream_len - c : 64;
        if (ref_find((const uint8_t *)stream + c, n, "needle_target") >= 0) naive_found++;
    }
    printf("Naive fixed-chunk matches: %zu\n", naive_found);
    CHECK(naive_found == 0, "naive fixed-chunk scan misses the straddling needle");

    printf("Rolling buffer chunk capacity: 64 bytes\n");
    printf("Rolling buffer total matches found: %zu\n", found);
    for (size_t m = 0; m < found; m++) {
        printf("Match %zu: line %zu, byte offset %zu: \"%s\"\n",
            m + 1,
            results[m].line_number,
            results[m].byte_offset,
            results[m].line_snippet
        );
    }
    CHECK(found == 1, "rolling: exactly one match");
    CHECK(found == 1 && results[0].line_number == oracle_line && oracle_line == 2, "rolling: line number equals oracle");
    CHECK(found == 1 && (long)results[0].byte_offset == needle_off, "rolling: byte offset equals oracle");
    /* The roll must give the same answer for any chunk capacity that holds the longest line. */
    for (size_t cap = 64; cap <= 200; cap++) {
        MatchResult rr[4];
        size_t nf = search_stream_rolling_buffer((const uint8_t *)stream, stream_len, cap, "needle_target", rr, 4);
        CHECK(nf == 1 && rr[0].line_number == oracle_line && (long)rr[0].byte_offset == needle_off,
              "rolling sweep: result independent of chunk capacity");
    }

    /* --------------------------------------------------------------------- */
    /* Test 4: Byte-oriented bstr vs UTF-8 Validation                        */
    /* --------------------------------------------------------------------- */
    printf("\n--- Phase 4: Byte-Oriented bstr Scanning ---\n");
    /* Create a buffer containing invalid UTF-8 (e.g. 0xFF, 0xFE, 0xC0) plus ASCII target */
    uint8_t bstr_corpus[64];
    memset(bstr_corpus, 'x', sizeof(bstr_corpus));
    bstr_corpus[10] = 0xFF; /* Invalid UTF-8 byte */
    bstr_corpus[11] = 0xFE; /* Invalid UTF-8 byte */
    bstr_corpus[12] = 0xC0; /* Invalid UTF-8 start byte (overlong) */
    memcpy(bstr_corpus + 20, "TARGET_TOKEN", 12);

    bool utf8_valid = is_valid_utf8(bstr_corpus, sizeof(bstr_corpus));
    printf("UTF-8 strict validation result: %s\n", utf8_valid ? "VALID" : "INVALID");

    /* Raw byte search succeeds despite invalid UTF-8 */
    const uint8_t *needle = (const uint8_t *)"TARGET_TOKEN";
    size_t needle_len = 12;
    int found_raw_offset = -1;
    for (size_t i = 0; i + needle_len <= sizeof(bstr_corpus); i++) {
        if (memcmp(bstr_corpus + i, needle, needle_len) == 0) {
            found_raw_offset = (int)i;
            break;
        }
    }
    printf("Byte-oriented raw match found: %s at offset %d\n",
        found_raw_offset >= 0 ? "YES" : "NO", found_raw_offset);

    CHECK(!utf8_valid, "utf8: corpus with 0xFF 0xFE 0xC0 is invalid");
    CHECK(found_raw_offset == 20, "bstr: raw search finds the token at offset 20");
    /* Negative control: with the three bad bytes repaired the same buffer validates, so the INVALID
     * verdict above comes from those bytes and not from a buggy validator. */
    uint8_t repaired[64];
    memcpy(repaired, bstr_corpus, sizeof(repaired));
    repaired[10] = repaired[11] = repaired[12] = 'x';
    CHECK(is_valid_utf8(repaired, sizeof(repaired)), "utf8: repaired buffer is valid");
    /* Each bad byte on its own must be rejected: 0xFF and 0xFE are never valid, 0xC0 is an overlong lead. */
    const uint8_t bad_bytes[3] = {0xFF, 0xFE, 0xC0};
    for (size_t b = 0; b < 3; b++) {
        uint8_t one[64];
        memset(one, 'x', sizeof(one));
        one[10] = bad_bytes[b];
        CHECK(!is_valid_utf8(one, sizeof(one)), "utf8: each bad byte alone is rejected");
    }
    /* 0xC0 0x80 is the classic overlong NUL: the continuation byte is well formed, so only the lead-byte rule rejects it. */
    uint8_t overlong[64];
    memset(overlong, 'x', sizeof(overlong));
    overlong[10] = 0xC0;
    overlong[11] = 0x80;
    CHECK(!is_valid_utf8(overlong, sizeof(overlong)), "utf8: overlong 0xC0 0x80 is rejected");

    if (check_failures != 0) {
        fprintf(stderr, "%d of %d checks failed\n", check_failures, check_count);
        return 1;
    }
    printf("\nVerification: ALL MECHANICS VERIFIED\n");
    return 0;
}
