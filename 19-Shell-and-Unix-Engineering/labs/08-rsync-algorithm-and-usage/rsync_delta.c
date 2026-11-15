/*
 * rsync_delta.c - a small, faithful model of the rsync delta-transfer algorithm.
 *
 * The weak checksum, its rolling update, the 16-bit tag hash, and the block-size
 * and strong-sum-length heuristic are transcribed from rsync's checksum.c
 * (get_checksum1), match.c (hash_search, SUM2HASH2), and generator.c
 * (sum_sizes_sqroot).  The strong checksum is a stand-in: 64-bit FNV-1a instead
 * of the MD5 / xxh128 / xxh3 digest that real rsync negotiates, because this lab
 * must build with nothing but a C compiler.
 *
 * Modes (all output is one "key=value" line per fact):
 *   gen SEED LEN OUT          write LEN pseudo-random bytes (xorshift32) to OUT
 *   weak FILE                 get_checksum1 over the whole file, as an unsigned decimal
 *   strong FILE               the stand-in strong checksum of the whole file, in hex
 *   roll FILE BLEN            roll a BLEN window over FILE; compare every rolled value
 *                             with a fresh get_checksum1 of the same window
 *   sizes LEN                 rsync's block length, strong-sum length, block count, and
 *                             signature bytes on the wire for a basis file of LEN bytes
 *   sig BASIS BLEN SIGFILE    the receiver's side: block signatures of BASIS
 *   delta SIGFILE NEW DELTA   the sender's side: search NEW against the signatures
 *   patch BASIS DELTA OUT     the receiver's side: rebuild NEW from BASIS plus DELTA
 *   naive BASIS NEW BLEN      count blocks that match only at the same offset
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef signed char schar;

#define BLOCK_SIZE 700
#define MAX_BLOCK_SIZE ((int32_t)1 << 17)
#define SUM_LENGTH 16
#define SHORT_SUM_LENGTH 2
#define BLOCKSUM_BIAS 10
#define TABLESIZE (1 << 16)
#define SUM2HASH2(s1, s2) (((s1) + (s2)) & 0xFFFF)

static void die(const char *what)
{
  fprintf(stderr, "rsync_delta: %s: %s\n", what, errno ? strerror(errno) : "error");
  exit(2);
}

/* Read a whole file into memory; returns the buffer and stores the length. */
static unsigned char *slurp(const char *path, long *len_out)
{
  FILE *f = fopen(path, "rb");
  unsigned char *buf = NULL;
  long cap = 0, len = 0;
  size_t got;

  if (f == NULL)
    die(path);
  for (;;) {
    if (len == cap) {
      cap = cap ? cap * 2 : 65536;
      buf = realloc(buf, (size_t)cap);
      if (buf == NULL)
        die("realloc");
    }
    got = fread(buf + len, 1, (size_t)(cap - len), f);
    len += (long)got;
    if (got == 0)
      break;
  }
  if (ferror(f))
    die(path);
  fclose(f);
  *len_out = len;
  return buf;
}

/* Transcribed from rsync checksum.c get_checksum1() with CHAR_OFFSET 0.
 * Bytes are read as signed char, exactly as rsync does. */
static uint32_t get_checksum1(const unsigned char *buf1, int32_t len)
{
  int32_t i;
  uint32_t s1, s2;
  const schar *buf = (const schar *)buf1;

  s1 = s2 = 0;
  for (i = 0; i < (len - 4); i += 4) {
    s2 += 4 * (s1 + buf[i]) + 3 * buf[i + 1] + 2 * buf[i + 2] + buf[i + 3];
    s1 += (buf[i + 0] + buf[i + 1] + buf[i + 2] + buf[i + 3]);
  }
  for (; i < len; i++) {
    s1 += buf[i];
    s2 += s1;
  }
  return (s1 & 0xffff) + (s2 << 16);
}

/* Stand-in strong checksum: 64-bit FNV-1a. */
static uint64_t strong_sum(const unsigned char *p, long n)
{
  uint64_t h = 14695981039346656037ULL;
  long i;

  for (i = 0; i < n; i++) {
    h ^= p[i];
    h *= 1099511628211ULL;
  }
  return h;
}

/* Transcribed from rsync generator.c sum_sizes_sqroot() for protocol >= 30, with
 * csum_length = SHORT_SUM_LENGTH (the first pass) and a 16-byte negotiated digest. */
static void sum_sizes(int64_t len, int32_t *blength_out, int *s2length_out)
{
  int32_t blength, c;
  int s2length, b;
  int64_t l;

  if (len <= (int64_t)BLOCK_SIZE * BLOCK_SIZE)
    blength = BLOCK_SIZE;
  else {
    for (c = 1, l = len; l >>= 2; c <<= 1) {
    }
    if (c < 0 || c >= MAX_BLOCK_SIZE)
      blength = MAX_BLOCK_SIZE;
    else {
      blength = 0;
      do {
        blength |= c;
        if (len < (int64_t)blength * blength)
          blength &= ~c;
        c >>= 1;
      } while (c >= 8); /* round to multiple of 8 */
      if (blength < BLOCK_SIZE)
        blength = BLOCK_SIZE;
    }
  }

  b = BLOCKSUM_BIAS;
  for (l = len; l >>= 1; b += 2) {
  }
  for (c = blength; (c >>= 1) && b; b--) {
  }
  s2length = (b + 1 - 32 + 7) / 8;
  if (s2length < SHORT_SUM_LENGTH)
    s2length = SHORT_SUM_LENGTH;
  if (s2length > SUM_LENGTH)
    s2length = SUM_LENGTH;

  *blength_out = blength;
  *s2length_out = s2length;
}

static void put32(FILE *f, uint32_t v)
{
  unsigned char b[4];

  b[0] = (unsigned char)v;
  b[1] = (unsigned char)(v >> 8);
  b[2] = (unsigned char)(v >> 16);
  b[3] = (unsigned char)(v >> 24);
  if (fwrite(b, 1, 4, f) != 4)
    die("fwrite");
}

static void put64(FILE *f, uint64_t v)
{
  put32(f, (uint32_t)v);
  put32(f, (uint32_t)(v >> 32));
}

static uint32_t get32(const unsigned char *p)
{
  return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t get64(const unsigned char *p)
{
  return (uint64_t)get32(p) | (uint64_t)get32(p + 4) << 32;
}

/* Signature file: blen, count, then per block: weak (4), strong (8), len (4). */
static int mode_sig(const char *basis, long blen, const char *sigpath)
{
  long n, off, count = 0;
  unsigned char *x = slurp(basis, &n);
  FILE *f = fopen(sigpath, "wb");

  if (f == NULL)
    die(sigpath);
  if (blen <= 0)
    die("block length must be positive");
  count = (n + blen - 1) / blen;
  put32(f, (uint32_t)blen);
  put32(f, (uint32_t)count);
  for (off = 0; off < n; off += blen) {
    long l = n - off < blen ? n - off : blen;
    put32(f, get_checksum1(x + off, (int32_t)l));
    put64(f, strong_sum(x + off, l));
    put32(f, (uint32_t)l);
  }
  if (fclose(f) != 0)
    die(sigpath);
  printf("blocks=%ld\n", count);
  free(x);
  return 0;
}

struct delta_out {
  FILE *f;
  long literal_bytes;
  long matched_blocks;
  long tokens;
};

static void emit_literal(struct delta_out *d, const unsigned char *p, long n)
{
  if (n <= 0)
    return;
  if (fputc('L', d->f) == EOF)
    die("fputc");
  put32(d->f, (uint32_t)n);
  if (fwrite(p, 1, (size_t)n, d->f) != (size_t)n)
    die("fwrite");
  d->literal_bytes += n;
  d->tokens++;
}

static void emit_copy(struct delta_out *d, long idx)
{
  if (fputc('C', d->f) == EOF)
    die("fputc");
  put32(d->f, (uint32_t)idx);
  d->matched_blocks++;
  d->tokens++;
}

/* The sender: the search loop of rsync match.c hash_search(), simplified
 * (no in-place alignment, no want_i run-length preference, no chain cap). */
static int mode_delta(const char *sigpath, const char *newpath, const char *deltapath)
{
  long siglen, n, count, blen, i, off, last_match = 0, end, k;
  unsigned char *sig = slurp(sigpath, &siglen);
  unsigned char *x = slurp(newpath, &n);
  uint32_t *sum1;
  uint64_t *sum2;
  long *blk_len, *chain, *table;
  long hash_hits = 0, false_alarms = 0;
  uint32_t s1 = 0, s2 = 0, sum;
  struct delta_out d;

  if (siglen < 8)
    die("short signature");
  blen = (long)get32(sig);
  count = (long)get32(sig + 4);
  if (siglen != 8 + count * 16)
    die("bad signature size");
  sum1 = calloc((size_t)count + 1, sizeof *sum1);
  sum2 = calloc((size_t)count + 1, sizeof *sum2);
  blk_len = calloc((size_t)count + 1, sizeof *blk_len);
  chain = calloc((size_t)count + 1, sizeof *chain);
  table = malloc(TABLESIZE * sizeof *table);
  if (!sum1 || !sum2 || !blk_len || !chain || !table)
    die("alloc");
  for (i = 0; i < TABLESIZE; i++)
    table[i] = -1;
  for (i = 0; i < count; i++) {
    const unsigned char *p = sig + 8 + i * 16;
    uint32_t t;

    sum1[i] = get32(p);
    sum2[i] = get64(p + 4);
    blk_len[i] = (long)get32(p + 12);
    t = SUM2HASH2(sum1[i] & 0xFFFF, sum1[i] >> 16);
    chain[i] = table[t];
    table[t] = i;
  }

  d.f = fopen(deltapath, "wb");
  if (d.f == NULL)
    die(deltapath);
  d.literal_bytes = d.matched_blocks = d.tokens = 0;
  /* Like rsync's sender, echo the block length back ahead of the tokens. */
  put32(d.f, (uint32_t)blen);

  if (n > 0 && count > 0) {
    end = n + 1 - blk_len[count - 1];
    k = n < blen ? n : blen;
    sum = get_checksum1(x, (int32_t)k);
    s1 = sum & 0xFFFF;
    s2 = sum >> 16;
    off = 0;
    while (off < end) {
      long l = n - off < blen ? n - off : blen;
      int matched = 0;
      int have_strong = 0;
      uint64_t strong = 0;

      sum = (s1 & 0xffff) | (s2 << 16);
      i = table[SUM2HASH2(s1, s2)];
      if (i >= 0)
        hash_hits++;
      for (; i >= 0; i = chain[i]) {
        if (sum != sum1[i] || l != blk_len[i])
          continue;
        if (!have_strong) {
          strong = strong_sum(x + off, l);
          have_strong = 1;
        }
        if (strong != sum2[i]) {
          false_alarms++;
          continue;
        }
        emit_literal(&d, x + last_match, off - last_match);
        emit_copy(&d, i);
        off += l;
        last_match = off;
        matched = 1;
        break;
      }
      if (matched) {
        if (off >= end)
          break;
        k = n - off < blen ? n - off : blen;
        sum = get_checksum1(x + off, (int32_t)k);
        s1 = sum & 0xFFFF;
        s2 = sum >> 16;
        continue;
      }
      /* Roll: drop x[off], add x[off + k] (or shrink the window at the tail). */
      s1 -= (uint32_t)(int32_t)(schar)x[off];
      s2 -= (uint32_t)k * (uint32_t)(int32_t)(schar)x[off];
      if (off + k < n) {
        s1 += (uint32_t)(int32_t)(schar)x[off + k];
        s2 += s1;
      } else
        k--;
      off++;
    }
  }
  emit_literal(&d, x + last_match, n - last_match);
  if (fclose(d.f) != 0)
    die(deltapath);

  printf("blocks=%ld\n", count);
  printf("matched_blocks=%ld\n", d.matched_blocks);
  printf("literal_bytes=%ld\n", d.literal_bytes);
  printf("hash_hits=%ld\n", hash_hits);
  printf("false_alarms=%ld\n", false_alarms);
  free(sig);
  free(x);
  free(sum1);
  free(sum2);
  free(blk_len);
  free(chain);
  free(table);
  return 0;
}

/* The receiver: rebuild the new file from the basis and the token stream. */
static int mode_patch(const char *basis, const char *deltapath, const char *outpath)
{
  long bn, dn, p = 0;
  unsigned char *b = slurp(basis, &bn);
  unsigned char *dl = slurp(deltapath, &dn);
  FILE *out = fopen(outpath, "wb");
  long blen;

  if (out == NULL)
    die(outpath);
  if (dn < 4)
    die("short delta");
  blen = (long)get32(dl);
  p = 4;
  if (blen <= 0)
    die("bad block length in delta");
  while (p < dn) {
    unsigned char tag = dl[p++];
    uint32_t v;

    if (p + 4 > dn)
      die("truncated delta");
    v = get32(dl + p);
    p += 4;
    if (tag == 'L') {
      if (p + (long)v > dn)
        die("truncated literal");
      if (fwrite(dl + p, 1, v, out) != v)
        die("fwrite");
      p += (long)v;
    } else if (tag == 'C') {
      long start = (long)v * blen;
      long l = bn - start < blen ? bn - start : blen;

      if (start >= bn || l <= 0)
        die("copy token beyond basis");
      if (fwrite(b + start, 1, (size_t)l, out) != (size_t)l)
        die("fwrite");
    } else
      die("bad token");
  }
  if (fclose(out) != 0)
    die(outpath);
  printf("delta_bytes=%ld\n", dn);
  free(b);
  free(dl);
  return 0;
}

static int mode_naive(const char *basis, const char *newpath, long blen)
{
  long bn, nn, off, same = 0, total = 0;
  unsigned char *b = slurp(basis, &bn);
  unsigned char *x = slurp(newpath, &nn);

  for (off = 0; off < nn; off += blen) {
    long l = nn - off < blen ? nn - off : blen;

    total++;
    if (off + l <= bn && memcmp(b + off, x + off, (size_t)l) == 0)
      same++;
  }
  printf("aligned_matches=%ld\n", same);
  printf("new_blocks=%ld\n", total);
  free(b);
  free(x);
  return 0;
}

static int mode_roll(const char *path, long blen)
{
  long n, off, checked = 0, mismatches = 0;
  unsigned char *x = slurp(path, &n);
  uint32_t sum, s1, s2;

  if (blen <= 0 || blen > n)
    die("block length must be in 1..file size");
  sum = get_checksum1(x, (int32_t)blen);
  s1 = sum & 0xFFFF;
  s2 = sum >> 16;
  for (off = 0;; off++) {
    uint32_t rolled = (s1 & 0xffff) | (s2 << 16);

    checked++;
    if (rolled != get_checksum1(x + off, (int32_t)blen))
      mismatches++;
    if (off + blen >= n)
      break;
    s1 -= (uint32_t)(int32_t)(schar)x[off];
    s2 -= (uint32_t)blen * (uint32_t)(int32_t)(schar)x[off];
    s1 += (uint32_t)(int32_t)(schar)x[off + blen];
    s2 += s1;
  }
  printf("windows=%ld\n", checked);
  printf("mismatches=%ld\n", mismatches);
  free(x);
  return 0;
}

static int mode_gen(unsigned long seed, long len, const char *outpath)
{
  FILE *f = fopen(outpath, "wb");
  uint32_t s = (uint32_t)seed ? (uint32_t)seed : 1u;
  long i;

  if (f == NULL)
    die(outpath);
  for (i = 0; i < len; i++) {
    s ^= s << 13;
    s ^= s >> 17;
    s ^= s << 5;
    if (fputc((int)(s >> 24), f) == EOF)
      die("fputc");
  }
  if (fclose(f) != 0)
    die(outpath);
  return 0;
}

int main(int argc, char **argv)
{
  if (argc >= 5 && strcmp(argv[1], "gen") == 0)
    return mode_gen(strtoul(argv[2], NULL, 10), atol(argv[3]), argv[4]);
  if (argc == 3 && strcmp(argv[1], "weak") == 0) {
    long n;
    unsigned char *x = slurp(argv[2], &n);

    printf("weak=%lu\n", (unsigned long)get_checksum1(x, (int32_t)n));
    free(x);
    return 0;
  }
  if (argc == 3 && strcmp(argv[1], "strong") == 0) {
    long n;
    unsigned char *x = slurp(argv[2], &n);

    printf("strong=%016llx\n", (unsigned long long)strong_sum(x, n));
    free(x);
    return 0;
  }
  if (argc == 4 && strcmp(argv[1], "roll") == 0)
    return mode_roll(argv[2], atol(argv[3]));
  if (argc == 3 && strcmp(argv[1], "sizes") == 0) {
    int64_t len = (int64_t)strtoll(argv[2], NULL, 10);
    int32_t blength;
    int s2length;
    int64_t count;

    sum_sizes(len, &blength, &s2length);
    count = len / blength + (len % blength != 0);
    printf("blength=%ld\n", (long)blength);
    printf("s2length=%d\n", s2length);
    printf("count=%lld\n", (long long)count);
    printf("sig_wire_bytes=%lld\n", (long long)(count * (4 + s2length)));
    return 0;
  }
  if (argc == 5 && strcmp(argv[1], "sig") == 0)
    return mode_sig(argv[2], atol(argv[3]), argv[4]);
  if (argc == 5 && strcmp(argv[1], "delta") == 0)
    return mode_delta(argv[2], argv[3], argv[4]);
  if (argc == 5 && strcmp(argv[1], "patch") == 0)
    return mode_patch(argv[2], argv[3], argv[4]);
  if (argc == 5 && strcmp(argv[1], "naive") == 0)
    return mode_naive(argv[2], argv[3], atol(argv[4]));
  fprintf(stderr, "usage: rsync_delta gen|weak|strong|roll|sizes|sig|delta|patch|naive ...\n");
  return 64;
}
