/*
 * shattered_detect_demo - Case Study Lab
 *
 * Uses the real sha1collisiondetection library (vendored in sha1dc/) and real public colliding
 * test vectors (vectors.h) to show, with computed results rather than narrative:
 *  1. The SHAttered chaining-value structure: equal after the prefix, different after the first
 *     near-collision block, equal again after the second.
 *  2. Git's "blob <size>\0" framing breaks the collision, and also hides it from sha1dc.
 *  3. A content-addressable store keyed by raw SHA-1 loses one of two colliding objects under a
 *     skip-if-present write rule, and notices it under a byte-compare rule.
 *  4. sha1dc flags the SHAttered and the SHA-1 is a Shambles pairs; the UBC check is only a filter.
 *  5. SHA-256 separates the same pairs.
 *
 * Every check prints "ok" or "FAILED"; the exit status is the number of failed checks.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sha1dc/sha1.h"
#include "sha1dc/ubc_check.h"
#include "vectors.h"

#define ROTL32(x, n) (((x) << (n)) | ((x) >> (32 - (n))))
#define ROTR32(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static int failures;

static void expect(int cond, const char *what) {
    printf("  check: %s: %s\n", what, cond ? "ok" : "FAILED");
    if (!cond) {
        failures++;
    }
}

static void print_hex(const unsigned char *bytes, size_t len) {
    for (size_t i = 0; i < len; i++) {
        printf("%02x", bytes[i]);
    }
}

/* --- SHA-1 through the vendored library --- */

typedef struct {
    int detect;     /* SHA1DCSetUseDetectColl */
    int use_ubc;    /* SHA1DCSetUseUBC */
    int safe_hash;  /* SHA1DCSetSafeHash */
} sha1_mode_t;

static const sha1_mode_t PLAIN = {0, 1, 0};   /* ordinary SHA-1, no detection */
static const sha1_mode_t DETECT = {1, 1, 1};  /* library defaults */

/* Hash n bytes; return the library's collision flag. */
static int sha1_run(const sha1_mode_t *m, const unsigned char *d, size_t n, unsigned char out[20]) {
    SHA1_CTX ctx;
    SHA1DCInit(&ctx);
    SHA1DCSetUseDetectColl(&ctx, m->detect);
    SHA1DCSetUseUBC(&ctx, m->use_ubc);
    SHA1DCSetSafeHash(&ctx, m->safe_hash);
    SHA1DCUpdate(&ctx, (const char *)d, n);
    return SHA1DCFinal(out, &ctx);
}

/* Smallest multiple of 64 bytes after which the library has already set its collision flag, or 0. */
static size_t first_flag_len(const unsigned char *d, size_t n) {
    for (size_t k = 64; k <= n; k += 64) {
        SHA1_CTX ctx;
        SHA1DCInit(&ctx);
        SHA1DCUpdate(&ctx, (const char *)d, k);
        if (ctx.found_collision) {
            return k;
        }
    }
    return 0;
}

/* Print the names of the disturbance vectors whose bit is set in mask, such as "II(52,0)". */
static void print_dv_names(uint32_t mask) {
    int any = 0;
    for (int i = 0; sha1_dvs[i].dvType != 0; i++) {
        if (mask & ((uint32_t)1 << sha1_dvs[i].maskb)) {
            printf("%s%s(%d,%d)", any ? " " : "", sha1_dvs[i].dvType == 1 ? "I" : "II", sha1_dvs[i].dvK,
                   sha1_dvs[i].dvB);
            any = 1;
        }
    }
    if (!any) {
        printf("none");
    }
}

/* Chaining value after the first n bytes, n a multiple of 64. */
static void sha1_cv(const unsigned char *d, size_t n, uint32_t cv[5]) {
    SHA1_CTX ctx;
    SHA1DCInit(&ctx);
    SHA1DCSetUseDetectColl(&ctx, 0);
    SHA1DCUpdate(&ctx, (const char *)d, n);
    memcpy(cv, ctx.ihv, sizeof(ctx.ihv));
}

static void print_cv(const uint32_t cv[5]) {
    printf("%08x%08x%08x%08x%08x", cv[0], cv[1], cv[2], cv[3], cv[4]);
}

/* Git blob framing: "blob <size>\0" followed by the payload. Returns the framed length. */
static size_t git_blob_frame(const unsigned char *payload, size_t n, unsigned char *out, size_t cap,
                             size_t *header_len) {
    char hdr[32];
    int h = snprintf(hdr, sizeof(hdr), "blob %zu", n);
    if (h < 0 || (size_t)h + 1 + n > cap) {
        return 0;
    }
    memcpy(out, hdr, (size_t)h);
    out[h] = '\0';
    memcpy(out + h + 1, payload, n);
    *header_len = (size_t)h + 1;
    return (size_t)h + 1 + n;
}

/* Expanded message words of the 64-byte block at p, as the library expects them for ubc_check. */
static void expand_block(const unsigned char *p, uint32_t W[80]) {
    for (int i = 0; i < 16; i++) {
        W[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) | ((uint32_t)p[i * 4 + 2] << 8) |
               (uint32_t)p[i * 4 + 3];
    }
    for (int i = 16; i < 80; i++) {
        W[i] = ROTL32(W[i - 3] ^ W[i - 8] ^ W[i - 14] ^ W[i - 16], 1);
    }
}

/* --- Toy content-addressable store keyed by the 20-byte SHA-1 of the stored bytes --- */

typedef struct {
    unsigned char key[20];
    unsigned char data[640];
    size_t len;
} cas_entry_t;

typedef struct {
    cas_entry_t entries[4];
    int count;
} cas_t;

typedef enum { PUT_STORED, PUT_SKIPPED_PRESENT, PUT_COLLISION_FOUND } put_result_t;

static const char *put_name(put_result_t r) {
    switch (r) {
    case PUT_STORED:
        return "stored";
    case PUT_SKIPPED_PRESENT:
        return "skipped (key present)";
    default:
        return "COLLISION FOUND";
    }
}

/* compare_bytes = 0 models the loose-object write rule (skip when the name exists);
 * compare_bytes = 1 models the index-pack rule (compare an incoming duplicate with the stored one). */
static put_result_t cas_put(cas_t *s, const unsigned char *data, size_t len, int compare_bytes) {
    unsigned char key[20];
    sha1_run(&PLAIN, data, len, key);
    for (int i = 0; i < s->count; i++) {
        if (memcmp(s->entries[i].key, key, 20) == 0) {
            if (compare_bytes && (s->entries[i].len != len || memcmp(s->entries[i].data, data, len) != 0)) {
                return PUT_COLLISION_FOUND;
            }
            return PUT_SKIPPED_PRESENT;
        }
    }
    cas_entry_t *e = &s->entries[s->count++];
    memcpy(e->key, key, 20);
    memcpy(e->data, data, len);
    e->len = len;
    return PUT_STORED;
}

/* --- SHA-256 (FIPS 180-4) --- */

typedef struct {
    uint32_t state[8];
    uint64_t count;
    uint8_t buffer[64];
} sha256_ctx_t;

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

static void sha256_init(sha256_ctx_t *ctx) {
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(ctx->state, iv, sizeof(iv));
    ctx->count = 0;
}

static void sha256_transform(sha256_ctx_t *ctx, const uint8_t data[64]) {
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2], d = ctx->state[3];
    uint32_t e = ctx->state[4], f = ctx->state[5], g = ctx->state[6], h = ctx->state[7];
    uint32_t W[64];

    for (int i = 0; i < 16; i++) {
        W[i] = ((uint32_t)data[i * 4] << 24) | ((uint32_t)data[i * 4 + 1] << 16) |
               ((uint32_t)data[i * 4 + 2] << 8) | ((uint32_t)data[i * 4 + 3]);
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROTR32(W[i - 15], 7) ^ ROTR32(W[i - 15], 18) ^ (W[i - 15] >> 3);
        uint32_t s1 = ROTR32(W[i - 2], 17) ^ ROTR32(W[i - 2], 19) ^ (W[i - 2] >> 10);
        W[i] = W[i - 16] + s0 + W[i - 7] + s1;
    }
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ROTR32(e, 6) ^ ROTR32(e, 11) ^ ROTR32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = h + S1 + ch + K256[i] + W[i];
        uint32_t S0 = ROTR32(a, 2) ^ ROTR32(a, 13) ^ ROTR32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    ctx->state[0] += a;
    ctx->state[1] += b;
    ctx->state[2] += c;
    ctx->state[3] += d;
    ctx->state[4] += e;
    ctx->state[5] += f;
    ctx->state[6] += g;
    ctx->state[7] += h;
}

static void sha256_update(sha256_ctx_t *ctx, const uint8_t *data, size_t len) {
    size_t idx = (ctx->count >> 3) & 63;
    ctx->count += ((uint64_t)len << 3);

    size_t part_len = 64 - idx;
    size_t i = 0;

    if (len >= part_len) {
        memcpy(&ctx->buffer[idx], data, part_len);
        sha256_transform(ctx, ctx->buffer);
        for (i = part_len; i + 63 < len; i += 64) {
            sha256_transform(ctx, &data[i]);
        }
        idx = 0;
    }
    memcpy(&ctx->buffer[idx], &data[i], len - i);
}

static void sha256_final(sha256_ctx_t *ctx, uint8_t digest[32]) {
    uint8_t final_count[8];
    for (int i = 0; i < 8; i++) {
        final_count[i] = (uint8_t)((ctx->count >> ((7 - i) * 8)) & 0xFF);
    }
    sha256_update(ctx, (const uint8_t *)"\x80", 1);
    while (((ctx->count >> 3) & 63) != 56) {
        sha256_update(ctx, (const uint8_t *)"\x00", 1);
    }
    sha256_update(ctx, final_count, 8);
    for (int i = 0; i < 8; i++) {
        digest[i * 4] = (uint8_t)((ctx->state[i] >> 24) & 0xFF);
        digest[i * 4 + 1] = (uint8_t)((ctx->state[i] >> 16) & 0xFF);
        digest[i * 4 + 2] = (uint8_t)((ctx->state[i] >> 8) & 0xFF);
        digest[i * 4 + 3] = (uint8_t)(ctx->state[i] & 0xFF);
    }
}

static void sha256_buf(const unsigned char *d, size_t n, unsigned char out[32]) {
    sha256_ctx_t c;
    sha256_init(&c);
    sha256_update(&c, d, n);
    sha256_final(&c, out);
}

/* First and last offset at which a and b differ; returns the number of differing bytes. */
static size_t diff_span(const unsigned char *a, const unsigned char *b, size_t n, size_t *first, size_t *last) {
    size_t count = 0;
    *first = 0;
    *last = 0;
    for (size_t i = 0; i < n; i++) {
        if (a[i] != b[i]) {
            if (count == 0) {
                *first = i;
            }
            *last = i;
            count++;
        }
    }
    return count;
}

int main(void) {
    const size_t N = sizeof(shattered_1);
    unsigned char h1[20], h2[20];

    printf("=== SHAttered SHA-1 Collision and Git Hardening Verification ===\n\n");

    /* Known-answer tests, so a broken hash cannot make the later sections pass by accident. */
    printf("[0] Known-answer tests:\n");
    {
        unsigned char d1[20], d2[32];
        sha1_run(&PLAIN, (const unsigned char *)"abc", 3, d1);
        expect(memcmp(d1, "\xa9\x99\x3e\x36\x47\x06\x81\x6a\xba\x3e\x25\x71\x78\x50\xc2\x6c\x9c\xd0\xd8\x9d", 20) == 0,
               "SHA-1(\"abc\") = a9993e36...");
        sha256_buf((const unsigned char *)"abc", 3, d2);
        expect(memcmp(d2, "\xba\x78\x16\xbf\x8f\x01\xcf\xea\x41\x41\x40\xde\x5d\xae\x22\x23"
                          "\xb0\x03\x61\xa3\x96\x17\x7a\x9c\xb4\x10\xff\x61\xf2\x00\x15\xad", 32) == 0,
               "SHA-256(\"abc\") = ba7816bf...");
    }
    printf("\n");

    /* ---------------------------------------------------------------------------------
     * [1] The identical-prefix structure of SHAttered, on the real first 320 bytes
     * --------------------------------------------------------------------------------- */
    printf("[1] SHAttered chaining values (first %zu bytes of shattered-1.pdf and shattered-2.pdf):\n", N);
    {
        size_t first, last;
        size_t ndiff = diff_span(shattered_1, shattered_2, N, &first, &last);
        printf("  Differing bytes: %zu, in offsets %zu-%zu\n", ndiff, first, last);
        expect(first >= 192 && last <= 319, "all differences lie in the two blocks after the 192-byte prefix");

        static const size_t marks[3] = {192, 256, 320};
        static const char *names[3] = {"CV_3 (after 192-byte prefix)", "CV_4 (after near-collision block 1)",
                                       "CV_5 (after near-collision block 2)"};
        int equal[3];
        for (int i = 0; i < 3; i++) {
            uint32_t a[5], b[5];
            sha1_cv(shattered_1, marks[i], a);
            sha1_cv(shattered_2, marks[i], b);
            equal[i] = memcmp(a, b, sizeof(a)) == 0;
            printf("  %s:\n    file 1: ", names[i]);
            print_cv(a);
            printf("\n    file 2: ");
            print_cv(b);
            printf("\n    equal : %s\n", equal[i] ? "yes" : "no");
        }
        expect(equal[0] == 1 && equal[1] == 0 && equal[2] == 1, "CV equal, then different, then equal again");

        sha1_run(&PLAIN, shattered_1, N, h1);
        sha1_run(&PLAIN, shattered_2, N, h2);
        printf("  Plain SHA-1 of the 320 bytes, file 1: ");
        print_hex(h1, 20);
        printf("\n  Plain SHA-1 of the 320 bytes, file 2: ");
        print_hex(h2, 20);
        printf("\n");
        expect(memcmp(h1, h2, 20) == 0, "the two 320-byte prefixes collide under SHA-1");
    }
    printf("\n");

    /* ---------------------------------------------------------------------------------
     * [2] Git blob framing shifts the blocks
     * --------------------------------------------------------------------------------- */
    printf("[2] Git blob framing of the same two prefixes:\n");
    {
        unsigned char f1[1024], f2[1024];
        size_t hl1 = 0, hl2 = 0;
        size_t fl1 = git_blob_frame(shattered_1, N, f1, sizeof(f1), &hl1);
        size_t fl2 = git_blob_frame(shattered_2, N, f2, sizeof(f2), &hl2);
        size_t first, last;
        diff_span(shattered_1, shattered_2, N, &first, &last);
        printf("  Header \"blob %zu\\0\" is %zu bytes\n", N, hl1);
        printf("  First differing byte: raw offset %zu (position %zu in its block), framed offset %zu (position %zu in its block)\n",
               first, first % 64, first + hl1, (first + hl1) % 64);

        unsigned char g1[20], g2[20];
        int c1 = sha1_run(&PLAIN, f1, fl1, g1);
        int c2 = sha1_run(&PLAIN, f2, fl2, g2);
        (void)c1;
        (void)c2;
        printf("  Git blob ID, file 1: ");
        print_hex(g1, 20);
        printf("\n  Git blob ID, file 2: ");
        print_hex(g2, 20);
        printf("\n");
        expect(hl1 == hl2 && fl1 == fl2, "both prefixes get the same header");
        expect(memcmp(g1, g2, 20) != 0, "framed IDs differ, so no collision inside Git");

        unsigned char raw_out[20], fr_out1[20], fr_out2[20];
        int raw1 = sha1_run(&DETECT, shattered_1, N, raw_out);
        int fr1 = sha1_run(&DETECT, f1, fl1, fr_out1);
        int fr2 = sha1_run(&DETECT, f2, fl2, fr_out2);
        printf("  sha1dc on the raw bytes     : %s\n", raw1 ? "collision attack detected" : "clean");
        printf("  sha1dc on the framed bytes  : file 1 %s, file 2 %s\n", fr1 ? "detected" : "clean",
               fr2 ? "detected" : "clean");
        expect(raw1 == 1, "sha1dc flags the aligned bytes");
        expect(fr1 == 0 && fr2 == 0, "sha1dc has nothing to flag once the framing shifts the blocks");
    }
    printf("\n");

    /* ---------------------------------------------------------------------------------
     * [3] A raw-SHA-1 content-addressable store and the two write rules
     * --------------------------------------------------------------------------------- */
    printf("[3] Content-addressable store keyed by raw SHA-1 (not Git framing):\n");
    {
        cas_t loose, packed;
        memset(&loose, 0, sizeof(loose));
        memset(&packed, 0, sizeof(packed));

        put_result_t l1 = cas_put(&loose, shattered_1, N, 0);
        put_result_t l2 = cas_put(&loose, shattered_2, N, 0);
        put_result_t p1 = cas_put(&packed, shattered_1, N, 1);
        put_result_t p2 = cas_put(&packed, shattered_2, N, 1);
        printf("  skip-if-present rule (models the loose write): put 1 -> %s, put 2 -> %s\n", put_name(l1), put_name(l2));
        printf("  byte-compare rule (models index-pack)        : put 1 -> %s, put 2 -> %s\n", put_name(p1), put_name(p2));

        unsigned char key2[20];
        sha1_run(&PLAIN, shattered_2, N, key2);
        const cas_entry_t *got = &loose.entries[0];
        int returns_second = memcmp(got->key, key2, 20) == 0 && memcmp(got->data, shattered_2, N) == 0;
        printf("  Lookup of the key for object 2 returns: %s\n", returns_second ? "object 2" : "object 1 (wrong bytes)");
        expect(l1 == PUT_STORED && l2 == PUT_SKIPPED_PRESENT, "skip rule silently keeps only the first object");
        expect(!returns_second, "a reader asking for object 2 gets object 1");
        expect(p1 == PUT_STORED && p2 == PUT_COLLISION_FOUND, "byte-compare rule reports the collision");

        /* A tree entry holds only the 20-byte name, so equal names make equal trees. */
        unsigned char tree1[64], tree2[64];
        size_t tl = 0;
        const char *entry = "100644 auth.c";
        size_t el = strlen(entry);
        memcpy(tree1, entry, el);
        tree1[el] = '\0';
        memcpy(tree1 + el + 1, loose.entries[0].key, 20);
        tl = el + 1 + 20;
        memcpy(tree2, entry, el);
        tree2[el] = '\0';
        memcpy(tree2 + el + 1, key2, 20);
        printf("  Tree body \"100644 auth.c\\0<20-byte name>\" is %zu bytes (header \"tree %zu\\0\")\n", tl, tl);
        expect(memcmp(tree1, tree2, tl) == 0, "the tree bytes for object 1 and object 2 are identical");
    }
    printf("\n");

    /* ---------------------------------------------------------------------------------
     * [4] sha1dc on the real colliding pairs
     * --------------------------------------------------------------------------------- */
    printf("[4] sha1dc (sha1collisiondetection) on colliding pairs:\n");
    {
        struct {
            const char *name;
            const unsigned char *data;
            size_t len;
        } v[4] = {
            {"shattered-1 (320 B)", shattered_1, sizeof(shattered_1)},
            {"shattered-2 (320 B)", shattered_2, sizeof(shattered_2)},
            {"shambles messageA (640 B)", shambles_a, sizeof(shambles_a)},
            {"shambles messageB (640 B)", shambles_b, sizeof(shambles_b)},
        };
        unsigned char plain[4][20], safe[4][20];
        int flagged[4];
        for (int i = 0; i < 4; i++) {
            sha1_run(&PLAIN, v[i].data, v[i].len, plain[i]);
            flagged[i] = sha1_run(&DETECT, v[i].data, v[i].len, safe[i]);
            printf("  %s:\n    plain SHA-1 : ", v[i].name);
            print_hex(plain[i], 20);
            printf("\n    sha1dc flag : %s", flagged[i] ? "collision attack detected" : "clean");
            if (flagged[i]) {
                printf(" (flag set after %zu bytes)", first_flag_len(v[i].data, v[i].len));
            }
            printf("\n    safe-hash   : ");
            print_hex(safe[i], 20);
            printf("\n");
        }
        expect(memcmp(plain[0], plain[1], 20) == 0, "SHAttered pair: plain SHA-1 collides");
        expect(memcmp(plain[2], plain[3], 20) == 0, "Shambles pair: plain SHA-1 collides");
        expect(flagged[0] && flagged[1], "SHAttered pair: both halves flagged");
        expect(flagged[2] && flagged[3], "Shambles pair: both halves flagged");
        expect(memcmp(safe[0], safe[1], 20) != 0 && memcmp(safe[2], safe[3], 20) != 0,
               "safe-hash separates each flagged pair");

        expect(first_flag_len(shattered_1, N) == 320, "SHAttered flag is set when the second near-collision block is done");

        /* The UBC check is a speed filter: it selects disturbance vectors, recompression decides. */
        printf("  UBC masks per 64-byte block of shattered-1 (vectors whose unavoidable bit conditions hold):\n");
        for (size_t b = 0; b < N / 64; b++) {
            uint32_t W[80], mask[DVMASKSIZE] = {0};
            expand_block(shattered_1 + b * 64, W);
            ubc_check(W, mask);
            printf("    block %zu (offset %3zu): 0x%08x  ", b, b * 64, mask[0]);
            print_dv_names(mask[0]);
            printf("\n");
        }
        {
            uint32_t W[80], m3[DVMASKSIZE] = {0}, m4[DVMASKSIZE] = {0}, m0[DVMASKSIZE] = {0};
            expand_block(shattered_1, W);
            ubc_check(W, m0);
            expand_block(shattered_1 + 3 * 64, W);
            ubc_check(W, m3);
            expand_block(shattered_1 + 4 * 64, W);
            ubc_check(W, m4);
            expect(m0[0] == 0 && m3[0] == 0x08000000u && m4[0] == 0x08000000u,
                   "only the two near-collision blocks pass, and only for II(52,0)");
        }
        printf("  UBC masks of the Shambles messageA blocks that pass for at least one vector:\n");
        for (size_t b = 0; b < sizeof(shambles_a) / 64; b++) {
            uint32_t W[80], mask[DVMASKSIZE] = {0};
            expand_block(shambles_a + b * 64, W);
            ubc_check(W, mask);
            if (mask[0] != 0) {
                printf("    block %zu (offset %3zu): 0x%08x  ", b, b * 64, mask[0]);
                print_dv_names(mask[0]);
                printf("\n");
            }
        }

        sha1_mode_t no_ubc = {1, 0, 1};
        sha1_mode_t no_detect = {0, 1, 1};
        unsigned char tmp[20];
        int f_no_ubc = sha1_run(&no_ubc, shattered_1, N, tmp);
        int f_no_detect = sha1_run(&no_detect, shattered_1, N, tmp);
        printf("  UBC filter off, detection on : %s\n", f_no_ubc ? "detected" : "clean");
        printf("  Detection (recompression) off: %s\n", f_no_detect ? "detected" : "clean");
        expect(f_no_ubc == 1, "disabling the UBC filter still detects the attack");
        expect(f_no_detect == 0, "disabling collision detection stops the flag");

        unsigned char benign[320];
        for (size_t i = 0; i < sizeof(benign); i++) {
            benign[i] = (unsigned char)((i * 7 + 3) & 0xff);
        }
        unsigned char bout[20];
        int fb = sha1_run(&DETECT, benign, sizeof(benign), bout);
        printf("  Benign 320 bytes             : %s\n", fb ? "detected" : "clean");
        expect(fb == 0, "ordinary data is not flagged");

        printf("  Git's git_SHA1DCFinal() turns the flag into:\n");
        printf("    fatal: SHA-1 appears to be part of a collision attack: ");
        print_hex(safe[0], 20);
        printf("\n");
    }
    printf("\n");

    /* ---------------------------------------------------------------------------------
     * [5] SHA-256 on the same pairs
     * --------------------------------------------------------------------------------- */
    printf("[5] SHA-256 of the same pairs (git init --object-format=sha256 uses SHA-256 object names):\n");
    {
        unsigned char a[32], b[32], c[32], d[32];
        sha256_buf(shattered_1, sizeof(shattered_1), a);
        sha256_buf(shattered_2, sizeof(shattered_2), b);
        sha256_buf(shambles_a, sizeof(shambles_a), c);
        sha256_buf(shambles_b, sizeof(shambles_b), d);
        printf("  shattered-1 : ");
        print_hex(a, 32);
        printf("\n  shattered-2 : ");
        print_hex(b, 32);
        printf("\n  messageA    : ");
        print_hex(c, 32);
        printf("\n  messageB    : ");
        print_hex(d, 32);
        printf("\n");
        expect(memcmp(a, b, 32) != 0, "SHAttered pair is separated by SHA-256");
        expect(memcmp(c, d, 32) != 0, "Shambles pair is separated by SHA-256");
    }

    printf("\nfailed checks: %d\n", failures);
    return failures;
}
