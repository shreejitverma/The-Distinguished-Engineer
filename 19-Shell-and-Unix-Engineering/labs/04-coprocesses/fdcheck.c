/*
 * fdcheck: report whether each descriptor number given is open in this (exec'd) process.
 *
 * Usage: fdcheck FD...
 * Prints "FD:open" or "FD:closed" for each argument, space separated, on one line.
 * It is an external program on purpose: a descriptor marked close-on-exec is open in the shell
 * but closed here, which no shell builtin can show.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
  int i;

  if (argc < 2) {
    fprintf(stderr, "usage: fdcheck FD...\n");
    return 2;
  }
  for (i = 1; i < argc; i++) {
    char *end;
    long fd = strtol(argv[i], &end, 10);
    int open_now;

    if (*argv[i] == '\0' || *end != '\0' || fd < 0 || fd > 1023) {
      fprintf(stderr, "fdcheck: bad descriptor '%s'\n", argv[i]);
      return 2;
    }
    errno = 0;
    open_now = fcntl((int)fd, F_GETFD) != -1;
    if (!open_now && errno != EBADF) {
      perror("fdcheck: fcntl");
      return 1;
    }
    printf("%s%ld:%s", i > 1 ? " " : "", fd, open_now ? "open" : "closed");
  }
  printf("\n");
  return 0;
}
