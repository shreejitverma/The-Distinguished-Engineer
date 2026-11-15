/*
 * chatter: the smallest stdio filter, so a test controls exactly which libc
 * calls run. It copies stdin to stdout with fgets and fputs.
 *
 *   chatter plain  copy lines; libc picks the buffering of stdout
 *   chatter own    call setvbuf(stdout, NULL, _IOFBF, BUFSIZ) in main first,
 *                  as programs that manage their own buffering do
 *   chatter die    copy lines, then die of SIGTERM instead of exiting,
 *                  as a process killed by timeout or kill does
 *   chatter order  write "out" to stdout, then "err" to stderr, and exit
 */
#include <signal.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
  const char *mode = argc > 1 ? argv[1] : "plain";
  char line[4096];

  if (strcmp(mode, "order") == 0) {
    fputs("out\n", stdout);
    fputs("err\n", stderr);
    return 0;
  }
  if (strcmp(mode, "own") == 0 && setvbuf(stdout, NULL, _IOFBF, BUFSIZ) != 0) {
    return 2;
  }
  while (fgets(line, sizeof line, stdin) != NULL) {
    fputs(line, stdout);
  }
  if (strcmp(mode, "die") == 0) {
    signal(SIGTERM, SIG_DFL);
    raise(SIGTERM);
  }
  return 0;
}
