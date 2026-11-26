/*
 * regprobe.c: ask this machine's C library what bash asks it, with no shell in between.
 *
 * Usage:
 *   regprobe match PATTERN STRING   compile PATTERN with regcomp(REG_EXTENDED), run regexec on
 *                                   STRING, and print the result in the shape bash exposes:
 *                                     status=0|1|2   (match, no match, regcomp rejected PATTERN)
 *                                     n=COUNT        (re_nsub + 1, printed only on a match)
 *                                     I=[TEXT]       one line per subexpression that took part
 *                                     I=<unset>      a subexpression regexec reported as -1
 *   regprobe limits                 print RE_DUP_MAX=VALUE from this C library's headers
 *   regprobe libc                   print libc=glibc, libc=apple, or libc=other (musl defines no
 *                                   macro of its own, so it reports other)
 *
 * Exit status: 0 when the probe ran (whatever the match result), 64 on a usage error,
 * 70 when an allocation or write fails.
 */
#include <limits.h>
#include <locale.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int usage(void) {
  fputs("usage: regprobe match PATTERN STRING | regprobe limits | regprobe libc\n", stderr);
  return 64;
}

static int probe_match(const char *pattern, const char *string) {
  regex_t re;
  regmatch_t *m;
  size_t i, count;
  int rc;

  rc = regcomp(&re, pattern, REG_EXTENDED);
  if (rc != 0) {
    /* bash returns 2 here and never reaches regexec (bash-5.3 lib/sh/shmatch.c, sh_regmatch). */
    return printf("status=2\n") < 0 ? 70 : 0;
  }
  count = re.re_nsub + 1;
  m = malloc(count * sizeof(*m));
  if (m == NULL) {
    regfree(&re);
    return 70;
  }
  rc = regexec(&re, string, count, m, 0);
  if (rc != 0) {
    free(m);
    regfree(&re);
    return printf("status=1\n") < 0 ? 70 : 0;
  }
  if (printf("status=0\nn=%lu\n", (unsigned long)count) < 0) {
    free(m);
    regfree(&re);
    return 70;
  }
  for (i = 0; i < count; i++) {
    if (m[i].rm_so < 0) {
      rc = printf("%lu=<unset>\n", (unsigned long)i);
    } else {
      rc = printf("%lu=[%.*s]\n", (unsigned long)i, (int)(m[i].rm_eo - m[i].rm_so),
                  string + m[i].rm_so);
    }
    if (rc < 0) {
      free(m);
      regfree(&re);
      return 70;
    }
  }
  free(m);
  regfree(&re);
  return 0;
}

int main(int argc, char **argv) {
  /* Match in the locale named by the environment, as bash does at startup. */
  setlocale(LC_ALL, "");
  if (argc == 2 && strcmp(argv[1], "limits") == 0) {
    return printf("RE_DUP_MAX=%ld\n", (long)RE_DUP_MAX) < 0 ? 70 : 0;
  }
  if (argc == 2 && strcmp(argv[1], "libc") == 0) {
#if defined(__GLIBC__)
    return printf("libc=glibc\n") < 0 ? 70 : 0;
#elif defined(__APPLE__)
    return printf("libc=apple\n") < 0 ? 70 : 0;
#else
    return printf("libc=other\n") < 0 ? 70 : 0;
#endif
  }
  if (argc == 4 && strcmp(argv[1], "match") == 0) {
    return probe_match(argv[2], argv[3]);
  }
  return usage();
}
