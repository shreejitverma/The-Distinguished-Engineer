/* regprobe: compile one POSIX regular expression with the C library's regcomp(3)
 * and report what regexec(3) finds, so BRE and ERE rules can be checked against
 * the libc itself instead of against one grep implementation.
 *
 * Usage:
 *   regprobe libc                    print glibc, apple, musl, or other
 *   regprobe dupmax                  print sysconf(_SC_RE_DUP_MAX)
 *   regprobe bre|ere PATTERN STRING  compile PATTERN (ERE adds REG_EXTENDED)
 *
 * The match mode prints one line:
 *   match SO-EO[ N=SO-EO ...]  byte offsets of the whole match and each subexpression
 *                              (-1--1 for a subexpression that did not participate)
 *   nomatch                    regexec returned REG_NOMATCH
 *   error REG_NAME             regcomp rejected PATTERN with that POSIX error code
 * Exit status: 0 on match, 1 on nomatch, 2 on a compile error, 3 on a usage error.
 */
#include <regex.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const char *libc_name(void) {
#if defined(__GLIBC__)
  return "glibc";
#elif defined(__APPLE__)
  return "apple";
#elif defined(__linux__) && !defined(__BIONIC__)
  /* musl deliberately defines no identifying macro; on Linux without glibc
     or bionic, the matrix only has musl (Alpine). */
  return "musl";
#else
  return "other";
#endif
}

static const char *error_name(int code) {
  switch (code) {
  case REG_BADPAT: return "REG_BADPAT";
  case REG_ECOLLATE: return "REG_ECOLLATE";
  case REG_ECTYPE: return "REG_ECTYPE";
  case REG_EESCAPE: return "REG_EESCAPE";
  case REG_ESUBREG: return "REG_ESUBREG";
  case REG_EBRACK: return "REG_EBRACK";
  case REG_EPAREN: return "REG_EPAREN";
  case REG_EBRACE: return "REG_EBRACE";
  case REG_BADBR: return "REG_BADBR";
  case REG_ERANGE: return "REG_ERANGE";
  case REG_ESPACE: return "REG_ESPACE";
  case REG_BADRPT: return "REG_BADRPT";
#ifdef REG_ESIZE
  case REG_ESIZE: return "REG_ESIZE";
#endif
#ifdef REG_BADMAX
  case REG_BADMAX: return "REG_BADMAX";
#endif
#ifdef REG_EMPTY
  case REG_EMPTY: return "REG_EMPTY";
#endif
  default: return "REG_other";
  }
}

int main(int argc, char **argv) {
  regex_t re;
  regmatch_t m[10];
  size_t i, nsub;
  int cflags, rc;

  if (argc == 2 && strcmp(argv[1], "libc") == 0) {
    printf("%s\n", libc_name());
    return 0;
  }
  if (argc == 2 && strcmp(argv[1], "dupmax") == 0) {
    printf("%ld\n", sysconf(_SC_RE_DUP_MAX));
    return 0;
  }
  if (argc != 4 || (strcmp(argv[1], "bre") != 0 && strcmp(argv[1], "ere") != 0)) {
    fprintf(stderr, "usage: regprobe libc | dupmax | bre|ere PATTERN STRING\n");
    return 3;
  }

  cflags = strcmp(argv[1], "ere") == 0 ? REG_EXTENDED : 0;
  rc = regcomp(&re, argv[2], cflags);
  if (rc != 0) {
    printf("error %s\n", error_name(rc));
    return 2;
  }
  rc = regexec(&re, argv[3], 10, m, 0);
  if (rc == REG_NOMATCH) {
    printf("nomatch\n");
    regfree(&re);
    return 1;
  }
  if (rc != 0) {
    printf("error %s\n", error_name(rc));
    regfree(&re);
    return 2;
  }
  printf("match %ld-%ld", (long)m[0].rm_so, (long)m[0].rm_eo);
  nsub = re.re_nsub < 9 ? re.re_nsub : 9;
  for (i = 1; i <= nsub; i++)
    printf(" %zu=%ld-%ld", i, (long)m[i].rm_so, (long)m[i].rm_eo);
  printf("\n");
  regfree(&re);
  return 0;
}
