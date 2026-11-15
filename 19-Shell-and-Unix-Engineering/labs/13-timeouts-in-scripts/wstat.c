/*
 * wstat: run a command and report how it ended, as the kernel's wait status
 * says, plus the wall time it took.
 *
 * Usage: wstat COMMAND [ARG...]
 * Prints one line: "exit N" or "signal N", then " elapsed=S" with S the
 * whole seconds elapsed (rounded down) on the monotonic clock.
 *
 * A shell's $? cannot tell "exited with 143" from "killed by SIGTERM"
 * (both read 143 in most shells), so timeout(1) status contracts need this.
 */
#include <errno.h>
#include <stdio.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv) {
  struct timespec t0, t1;
  int status;
  pid_t pid;

  if (argc < 2) {
    fprintf(stderr, "usage: wstat COMMAND [ARG...]\n");
    return 2;
  }
  if (clock_gettime(CLOCK_MONOTONIC, &t0) != 0) {
    perror("wstat: clock_gettime");
    return 2;
  }
  pid = fork();
  if (pid < 0) {
    perror("wstat: fork");
    return 2;
  }
  if (pid == 0) {
    execvp(argv[1], argv + 1);
    perror("wstat: execvp");
    _exit(127);
  }
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR) {
      perror("wstat: waitpid");
      return 2;
    }
  }
  if (clock_gettime(CLOCK_MONOTONIC, &t1) != 0) {
    perror("wstat: clock_gettime");
    return 2;
  }
  if (WIFEXITED(status)) {
    printf("exit %d", WEXITSTATUS(status));
  } else if (WIFSIGNALED(status)) {
    printf("signal %d", WTERMSIG(status));
  } else {
    printf("other %d", status);
  }
  printf(" elapsed=%ld\n", (long)(t1.tv_sec - t0.tv_sec - (t1.tv_nsec < t0.tv_nsec)));
  return 0;
}
