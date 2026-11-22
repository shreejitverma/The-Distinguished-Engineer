/*
 * utf8probe: ask the C library how the current locale decodes bytes.
 *
 * Every text utility that is "multibyte aware" (wc -m, sed '.', awk length,
 * the shell's ${#x}) ends up calling mbrtowc(3) or a private equivalent, so
 * this probe shows the ground truth those tools build on.
 *
 * Usage:
 *   utf8probe mbmax  LOCALE          -> libc=<name> mb_cur_max=<n>
 *   utf8probe decode LOCALE HEXBYTES -> one token per decoding step:
 *                                       U+XXXX/len, bad (EILSEQ, skip 1 byte),
 *                                       short (incomplete sequence at the end)
 *   utf8probe width  LOCALE HEXCP... -> wcwidth(3) of each code point
 *
 * LOCALE is passed to setlocale(LC_ALL, ...); the probe fails with status 2
 * when the locale does not exist, so a missing locale can never pass a test.
 */
/* glibc declares wcwidth(3) only under _XOPEN_SOURCE (it is an XSI function). */
#define _XOPEN_SOURCE 700
#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static const char *libc_name(void) {
#if defined(__GLIBC__)
  return "glibc";
#elif defined(__APPLE__)
  return "apple";
#else
  return "other";
#endif
}

static int hexval(int c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

/* Parse "c3a9" into bytes; returns the byte count or -1 on bad input. */
static long parse_hex(const char *hex, unsigned char *out, size_t cap) {
  size_t n = strlen(hex), i;
  if (n % 2 != 0 || n / 2 > cap) return -1;
  for (i = 0; i < n; i += 2) {
    int hi = hexval((unsigned char)hex[i]), lo = hexval((unsigned char)hex[i + 1]);
    if (hi < 0 || lo < 0) return -1;
    out[i / 2] = (unsigned char)(hi * 16 + lo);
  }
  return (long)(n / 2);
}

static int decode(const char *hex) {
  unsigned char buf[256];
  long n = parse_hex(hex, buf, sizeof buf);
  size_t pos = 0;
  mbstate_t st;
  const char *sep = "";

  if (n < 0) {
    fprintf(stderr, "utf8probe: bad hex string: %s\n", hex);
    return 2;
  }
  memset(&st, 0, sizeof st);
  while (pos < (size_t)n) {
    wchar_t wc = 0;
    size_t r = mbrtowc(&wc, (const char *)buf + pos, (size_t)n - pos, &st);
    if (r == (size_t)-1) {
      printf("%sbad", sep);
      memset(&st, 0, sizeof st); /* the state is undefined after EILSEQ */
      pos += 1;
    } else if (r == (size_t)-2) {
      printf("%sshort", sep);
      break;
    } else {
      if (r == 0) r = 1; /* a NUL byte decodes to L'\0' and consumes one byte */
      printf("%sU+%04lX/%lu", sep, (unsigned long)wc, (unsigned long)r);
      pos += r;
    }
    sep = " ";
  }
  printf("\n");
  return 0;
}

static int widths(int argc, char **argv) {
  int i;
  for (i = 0; i < argc; i++) {
    char *end;
    unsigned long cp = strtoul(argv[i], &end, 16);
    if (*end != '\0') {
      fprintf(stderr, "utf8probe: bad code point: %s\n", argv[i]);
      return 2;
    }
    printf("%sU+%04lX=%d", i ? " " : "", cp, wcwidth((wchar_t)cp));
  }
  printf("\n");
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: utf8probe mbmax|decode|width LOCALE [ARG...]\n");
    return 2;
  }
  if (setlocale(LC_ALL, argv[2]) == NULL) {
    fprintf(stderr, "utf8probe: locale %s is not available\n", argv[2]);
    return 2;
  }
  if (strcmp(argv[1], "mbmax") == 0 && argc == 3) {
    printf("libc=%s mb_cur_max=%lu\n", libc_name(), (unsigned long)MB_CUR_MAX);
    return 0;
  }
  if (strcmp(argv[1], "decode") == 0 && argc == 4) return decode(argv[3]);
  if (strcmp(argv[1], "width") == 0 && argc >= 4) return widths(argc - 3, argv + 3);
  fprintf(stderr, "utf8probe: unknown mode or wrong argument count\n");
  return 2;
}
