/* getopt_probe.c - show how this C library's getopt(3) and getopt_long(3) scan an argument vector.
 *
 * Usage: getopt_probe short|long OPTSTRING [ARG...]
 *
 * The probe parses ARG... with OPTSTRING (getopt_long also knows --verbose, --version, and
 * --output=FILE) and prints, in order:
 *   libc=glibc|apple|musl|other      the C library, from its predefined macros
 *   opt=C [arg=VALUE]                 one line per short option returned
 *   long=NAME [arg=VALUE]             one line per long option returned
 *   opt=? optopt=C|0                  an unknown or ambiguous option (0: a long option)
 *   opt=: optopt=C                    a missing option-argument with a leading ':' in OPTSTRING
 *   optind=N                          optind after getopt returned -1
 *   argv=<a> <b> ...                  the vector after parsing, which GNU-style parsers permute
 * Diagnostics from the library are turned off (opterr = 0) so only the probe writes output.
 */
#define _GNU_SOURCE 1
#include <getopt.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const struct option longopts[] = {
    {"verbose", no_argument, NULL, 'v'},
    {"version", no_argument, NULL, 'V'},
    {"output", required_argument, NULL, 'o'},
    {NULL, 0, NULL, 0},
};

static const char *libc_name(void) {
#if defined(__GLIBC__)
  return "glibc";
#elif defined(__APPLE__)
  return "apple";
#elif defined(__linux__)
  /* musl deliberately defines no identifying macro; in the matrix the only other Linux libc is musl. */
  return "musl";
#else
  return "other";
#endif
}

int main(int argc, char *argv[]) {
  if (argc < 3 || (strcmp(argv[1], "short") != 0 && strcmp(argv[1], "long") != 0)) {
    fprintf(stderr, "usage: getopt_probe short|long OPTSTRING [ARG...]\n");
    return 2;
  }
  const int use_long = strcmp(argv[1], "long") == 0;
  const char *optstring = argv[2];
  /* Parse the vector that starts at OPTSTRING, so OPTSTRING plays the role of argv[0]. */
  char **av = argv + 2;
  const int ac = argc - 2;

  printf("libc=%s\n", libc_name());
  opterr = 0;
  for (;;) {
    int idx = -1;
    optarg = NULL; /* not every libc clears optarg for an option without an argument */
    const int c = use_long ? getopt_long(ac, av, optstring, longopts, &idx) : getopt(ac, av, optstring);
    if (c == -1) {
      break;
    }
    if (c == '?' || c == ':') {
      if (optopt == 0) {
        printf("opt=%c optopt=0\n", c);
      } else {
        printf("opt=%c optopt=%c\n", c, optopt);
      }
    } else if (idx >= 0) {
      printf("long=%s%s%s\n", longopts[idx].name, optarg ? " arg=" : "", optarg ? optarg : "");
    } else {
      printf("opt=%c%s%s\n", c, optarg ? " arg=" : "", optarg ? optarg : "");
    }
  }
  printf("optind=%d\n", optind);
  printf("argv=");
  for (int i = 1; i < ac; i++) {
    printf("%s<%s>", i > 1 ? " " : "", av[i]);
  }
  printf("\n");
  return 0;
}
