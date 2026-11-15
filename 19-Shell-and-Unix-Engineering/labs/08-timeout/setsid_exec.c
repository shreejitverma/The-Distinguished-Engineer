/* setsid_exec.c: run a command in a new session and process group, like
 * util-linux setsid(1), which macOS does not ship.
 * Usage: setsid_exec CMD [ARG...]
 * A descendant started this way leaves the process group that timeout(1)
 * signals, so a group-based timeout cannot reach it. */
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc < 2) {
    fputs("usage: setsid_exec CMD [ARG...]\n", stderr);
    return 2;
  }
  if (setsid() < 0) {
    perror("setsid");
    return 125;
  }
  execvp(argv[1], argv + 1);
  perror("execvp");
  return 127;
}
