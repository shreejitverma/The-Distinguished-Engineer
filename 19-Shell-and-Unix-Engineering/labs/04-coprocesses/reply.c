/*
 * reply: answer every line read on standard input with "ack LINE" on standard output.
 *
 * Usage: reply full|line
 *   full  keeps the C library default: stdout is fully buffered when it is not a terminal,
 *         so answers stay in the process until the buffer fills or the program exits.
 *   line  calls setvbuf(stdout, NULL, _IOLBF, 0) first, so each answer is written at its newline.
 *
 * This is the shape of most filters (sed, awk, tr, grep, bc) when they run as a coprocess.
 */
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
  char buf[4096];

  if (argc != 2 || (strcmp(argv[1], "full") != 0 && strcmp(argv[1], "line") != 0)) {
    fprintf(stderr, "usage: reply full|line\n");
    return 2;
  }
  if (strcmp(argv[1], "line") == 0 && setvbuf(stdout, NULL, _IOLBF, 0) != 0) {
    perror("reply: setvbuf");
    return 1;
  }
  while (fgets(buf, sizeof buf, stdin) != NULL) {
    if (printf("ack %s", buf) < 0) {
      return 1;
    }
  }
  /* fclose flushes whatever the full-buffered mode was still holding. */
  return fclose(stdout) == 0 ? 0 : 1;
}
