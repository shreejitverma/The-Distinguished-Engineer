/*
 * regoff: compile PATTERN with regcomp(3), run regexec(3) once on STRING, and print
 * the offsets the C library reports in pmatch[0..re_nsub], so the match the library
 * selected (leftmost, longest, and the subexpression split) is visible.
 *
 * usage: regoff -L
 *        regoff [-E] [-M] [-X] PATTERN STRING
 *   -L  print the C library family this was built against (darwin, glibc, musl, other)
 *   -E  compile as an ERE (REG_EXTENDED); without it the pattern is a BRE
 *   -M  add REG_MINIMAL (POSIX.1-2024); exit 3 if <regex.h> does not define it
 *   -X  add REG_ENHANCED (macOS extension); exit 3 if <regex.h> does not define it
 *
 * output: "(so,eo)" for pmatch[0] then each subexpression, on one line (exit 0);
 *         "nomatch" (exit 1); "regcomp: REG_xxx" (exit 2); "... undefined" (exit 3).
 */
#include <regex.h>
#include <stdio.h>
#include <string.h>

#if defined(__APPLE__)
#define LIBC_FAMILY "darwin"
#elif defined(__GLIBC__)
#define LIBC_FAMILY "glibc"
#elif defined(__linux__)
/* musl defines no identifying macro on purpose; a non-glibc Linux libc is musl here. */
#define LIBC_FAMILY "musl"
#else
#define LIBC_FAMILY "other"
#endif

#define NSLOTS 16

static const char *code_name(int rc) {
  switch (rc) {
  case REG_NOMATCH: return "REG_NOMATCH";
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
  default: return "REG_other";
  }
}

int main(int argc, char **argv) {
  regex_t re;
  regmatch_t m[NSLOTS];
  int cflags = 0;
  int i = 1;
  int rc;
  size_t k;

  for (; i < argc && argv[i][0] == '-' && argv[i][1] != '\0'; i++) {
    if (strcmp(argv[i], "-L") == 0) {
      printf("%s\n", LIBC_FAMILY);
      return 0;
    } else if (strcmp(argv[i], "-E") == 0) {
      cflags |= REG_EXTENDED;
    } else if (strcmp(argv[i], "-M") == 0) {
#ifdef REG_MINIMAL
      cflags |= REG_MINIMAL;
#else
      printf("REG_MINIMAL undefined\n");
      return 3;
#endif
    } else if (strcmp(argv[i], "-X") == 0) {
#ifdef REG_ENHANCED
      cflags |= REG_ENHANCED;
#else
      printf("REG_ENHANCED undefined\n");
      return 3;
#endif
    } else {
      fprintf(stderr, "regoff: unknown option %s\n", argv[i]);
      return 64;
    }
  }
  if (argc - i != 2) {
    fprintf(stderr, "usage: regoff -L | regoff [-E] [-M] [-X] PATTERN STRING\n");
    return 64;
  }

  rc = regcomp(&re, argv[i], cflags);
  if (rc != 0) {
    printf("regcomp: %s\n", code_name(rc));
    return 2;
  }
  rc = regexec(&re, argv[i + 1], NSLOTS, m, 0);
  if (rc != 0) {
    printf("%s\n", rc == REG_NOMATCH ? "nomatch" : code_name(rc));
    regfree(&re);
    return 1;
  }
  for (k = 0; k <= re.re_nsub && k < NSLOTS; k++) {
    printf("(%ld,%ld)", (long)m[k].rm_so, (long)m[k].rm_eo);
  }
  printf("\n");
  regfree(&re);
  return 0;
}
