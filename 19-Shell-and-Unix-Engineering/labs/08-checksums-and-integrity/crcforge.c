/* crcforge.c: compute and forge the POSIX cksum CRC.
 *
 *   crcforge -p FILE           print the POSIX cksum value of FILE (CRC over data, then length)
 *   crcforge -n FILE           print the same CRC computed WITHOUT the length octets
 *   crcforge TARGET SOURCE     write SOURCE to stdout with its last four bytes replaced so that
 *                              `cksum` prints the same CRC and size for it as for TARGET
 *
 * The CRC is linear over GF(2): after a fixed prefix, the register state is an affine function
 * of the next four bytes, so the four replacement bytes are the solution of a 32x32 linear
 * system, found here by Gaussian elimination. TARGET and SOURCE must have the same length
 * (at least four bytes), so the length octets that cksum appends are identical too.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t table[256];

/* make_table: the MSB-first table for G(x) = 0x04c11db7, as in the POSIX cksum RATIONALE. */
static void make_table(void) {
  for (uint32_t i = 0; i < 256; i++) {
    uint32_t c = i << 24;
    for (int k = 0; k < 8; k++)
      c = (c & 0x80000000u) ? (c << 1) ^ 0x04c11db7u : c << 1;
    table[i] = c;
  }
}

static uint32_t crc_update(uint32_t s, const unsigned char *p, size_t n) {
  while (n--)
    s = (s << 8) ^ table[(s >> 24) ^ *p++];
  return s;
}

/* posix_cksum: the register after the data, extended by the length least significant octet
 * first using the fewest octets, then complemented (POSIX.1-2024 XCU cksum DESCRIPTION). */
static uint32_t posix_cksum(const unsigned char *p, size_t n, int with_length) {
  uint32_t s = crc_update(0, p, n);
  if (with_length) {
    for (size_t len = n; len != 0; len >>= 8) {
      unsigned char c = (unsigned char)(len & 0xff);
      s = crc_update(s, &c, 1);
    }
  }
  return ~s;
}

/* read_all: read a whole file (or a pipe) into a malloc'd buffer; exit 2 on any error. */
static unsigned char *read_all(const char *path, size_t *len) {
  FILE *f = fopen(path, "rb");
  if (f == NULL) {
    perror(path);
    exit(2);
  }
  size_t cap = 4096, n = 0;
  unsigned char *buf = malloc(cap);
  if (buf == NULL) {
    perror("malloc");
    exit(2);
  }
  for (;;) {
    if (n == cap) {
      cap *= 2;
      unsigned char *grown = realloc(buf, cap);
      if (grown == NULL) {
        perror("realloc");
        exit(2);
      }
      buf = grown;
    }
    size_t got = fread(buf + n, 1, cap - n, f);
    n += got;
    if (got == 0)
      break;
  }
  if (ferror(f)) {
    perror(path);
    exit(2);
  }
  if (fclose(f) != 0) {
    perror(path);
    exit(2);
  }
  *len = n;
  return buf;
}

/* pack: the four bytes that encode x, most significant byte first. */
static void pack(uint32_t x, unsigned char b[4]) {
  for (int j = 0; j < 4; j++)
    b[j] = (unsigned char)(x >> (8 * (3 - j)));
}

/* solve: find x with crc_update(0, pack(x), 4) == rhs; return 0 on success. */
static int solve(uint32_t rhs, uint32_t *x) {
  uint32_t col[32];
  for (int k = 0; k < 32; k++) {
    unsigned char b[4];
    pack(1u << k, b);
    col[k] = crc_update(0, b, 4);
  }
  /* Row r holds coefficient bit k of equation r in bit k, and the right-hand side in bit 32. */
  uint64_t rows[32];
  for (int r = 0; r < 32; r++) {
    rows[r] = (uint64_t)((rhs >> r) & 1u) << 32;
    for (int k = 0; k < 32; k++)
      rows[r] |= (uint64_t)((col[k] >> r) & 1u) << k;
  }
  int pivot_col[32];
  int rank = 0;
  for (int k = 0; k < 32 && rank < 32; k++) {
    int p = rank;
    while (p < 32 && !((rows[p] >> k) & 1u))
      p++;
    if (p == 32)
      continue;
    uint64_t tmp = rows[p];
    rows[p] = rows[rank];
    rows[rank] = tmp;
    for (int r = 0; r < 32; r++)
      if (r != rank && ((rows[r] >> k) & 1u))
        rows[r] ^= rows[rank];
    pivot_col[rank++] = k;
  }
  if (rank != 32)
    return -1;
  uint32_t v = 0;
  for (int i = 0; i < 32; i++)
    if ((rows[i] >> 32) & 1u)
      v |= 1u << pivot_col[i];
  *x = v;
  return 0;
}

int main(int argc, char **argv) {
  make_table();
  if (argc == 3 && (strcmp(argv[1], "-p") == 0 || strcmp(argv[1], "-n") == 0)) {
    size_t n;
    unsigned char *d = read_all(argv[2], &n);
    printf("%lu\n", (unsigned long)posix_cksum(d, n, argv[1][1] == 'p'));
    free(d);
    return 0;
  }
  if (argc != 3 || argv[1][0] == '-') {
    fprintf(stderr, "usage: crcforge -p FILE | -n FILE | TARGET SOURCE\n");
    return 2;
  }
  size_t tn, sn;
  unsigned char *t = read_all(argv[1], &tn);
  unsigned char *s = read_all(argv[2], &sn);
  if (tn != sn || sn < 4) {
    fprintf(stderr, "crcforge: TARGET and SOURCE need the same length, at least 4 bytes\n");
    return 2;
  }
  static const unsigned char zero4[4] = {0, 0, 0, 0};
  uint32_t want = crc_update(0, t, tn);
  uint32_t pre = crc_update(0, s, sn - 4);
  /* crc_update(pre, x, 4) == crc_update(pre, 0000, 4) ^ crc_update(0, x, 4), by linearity. */
  uint32_t x;
  if (solve(want ^ crc_update(pre, zero4, 4), &x) != 0) {
    fprintf(stderr, "crcforge: singular system (cannot happen for a degree-32 CRC)\n");
    return 3;
  }
  pack(x, s + sn - 4);
  if (crc_update(pre, s + sn - 4, 4) != want) {
    fprintf(stderr, "crcforge: self-check failed\n");
    return 3;
  }
  if (fwrite(s, 1, sn, stdout) != sn || fflush(stdout) != 0) {
    perror("crcforge: stdout");
    return 2;
  }
  free(t);
  free(s);
  return 0;
}
