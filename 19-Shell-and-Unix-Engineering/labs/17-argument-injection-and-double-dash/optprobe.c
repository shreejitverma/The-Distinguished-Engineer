/*
 * optprobe: show how this C library's getopt(3) and getopt_long(3) classify
 * an argument vector, the parser that most external utilities share.
 *
 * Usage: optprobe MODE [ARG...]
 *   MODE long       getopt_long with optstring "o:" (the default ordering)
 *   MODE long-plus  getopt_long with optstring "+o:" (stop at the first operand)
 *   MODE short      getopt with optstring "o:"
 * Long options: --checkpoint (no argument), --checkpoint-action=ACTION,
 * and --output=FILE (the same as -o FILE).
 *
 * Output: first "libc: NAME" (from compile-time macros), then one line per
 * parsed element: "opt: NAME[=VALUE]", "error", or "operand: <ARG>".
 */
#define _GNU_SOURCE 1
#include <getopt.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const char *libc_name(void)
{
#if defined(__GLIBC__)
  return "glibc";
#elif defined(__APPLE__)
  return "apple";
#elif defined(__linux__)
  /* musl deliberately defines no identifying macro; on Linux it is the
   * remaining libc in the lab images. */
  return "musl";
#else
  return "other";
#endif
}

int main(int argc, char **argv)
{
  static const struct option longopts[] = {
    {"checkpoint", no_argument, NULL, 'C'},
    {"checkpoint-action", required_argument, NULL, 'A'},
    {"output", required_argument, NULL, 'o'},
    {NULL, 0, NULL, 0},
  };
  const char *mode;
  int use_long;
  const char *optstring;
  int c;

  if (argc < 2) {
    fprintf(stderr, "usage: optprobe long|long-plus|short [ARG...]\n");
    return 64;
  }
  mode = argv[1];
  if (strcmp(mode, "long") == 0) {
    use_long = 1;
    optstring = "o:";
  } else if (strcmp(mode, "long-plus") == 0) {
    use_long = 1;
    optstring = "+o:";
  } else if (strcmp(mode, "short") == 0) {
    use_long = 0;
    optstring = "o:";
  } else {
    fprintf(stderr, "optprobe: unknown mode %s\n", mode);
    return 64;
  }

  printf("libc: %s\n", libc_name());

  /* Parse argv[1..] as if MODE were the program name. */
  argc -= 1;
  argv += 1;
  opterr = 0;
  for (;;) {
    if (use_long)
      c = getopt_long(argc, argv, optstring, longopts, NULL);
    else
      c = getopt(argc, argv, optstring);
    if (c == -1)
      break;
    switch (c) {
    case 'C':
      printf("opt: checkpoint\n");
      break;
    case 'A':
      printf("opt: checkpoint-action=%s\n", optarg);
      break;
    case 'o':
      printf("opt: o=%s\n", optarg);
      break;
    default:
      printf("error\n");
      break;
    }
  }
  for (; optind < argc; optind++)
    printf("operand: <%s>\n", argv[optind]);
  return 0;
}
