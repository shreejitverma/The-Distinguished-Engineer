/*
 * bounded.c - run a command with a wall-clock bound, like GNU timeout.
 *
 * Usage: bounded SECONDS COMMAND [ARG...]
 * Exit status: the command's own status; 124 if it was still running after SECONDS
 * (it is then killed with SIGKILL and reaped); 125 if it could not be started;
 * 128 + N if it died of signal N.
 * The command runs in its own process group so that the kill reaches its children too.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t expired;

static void on_alarm(int sig) {
  (void)sig;
  expired = 1;
}

int main(int argc, char **argv) {
  struct sigaction sa;
  char *end;
  long secs;
  pid_t pid;
  pid_t waited = 0;
  int status;
  const struct timespec interval = {0, 20 * 1000 * 1000};

  if (argc < 3) {
    fprintf(stderr, "usage: bounded SECONDS COMMAND [ARG...]\n");
    return 125;
  }
  secs = strtol(argv[1], &end, 10);
  if (*argv[1] == '\0' || *end != '\0' || secs <= 0) {
    fprintf(stderr, "bounded: bad SECONDS: %s\n", argv[1]);
    return 125;
  }
  pid = fork();
  if (pid < 0) {
    perror("bounded: fork");
    return 125;
  }
  if (pid == 0) {
    if (setpgid(0, 0) < 0) {
      perror("bounded: setpgid");
      _exit(125);
    }
    execvp(argv[2], argv + 2);
    perror("bounded: exec");
    _exit(125);
  }
  sa.sa_handler = on_alarm;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  if (sigaction(SIGALRM, &sa, NULL) < 0) {
    perror("bounded: sigaction");
    return 125;
  }
  alarm((unsigned)secs);
  while (!expired) {
    /* A signal delivered just before waitpid must never leave it blocked. */
    waited = waitpid(pid, &status, WNOHANG);
    if (waited == pid) {
      break;
    }
    if (waited < 0 && errno != EINTR) {
      perror("bounded: waitpid");
      return 125;
    }
    nanosleep(&interval, NULL);
  }
  alarm(0);
  if (waited != pid) {
    /* Kill the group; fall back to the pid if the child had not yet made its group. */
    if (kill(-pid, SIGKILL) < 0 && kill(pid, SIGKILL) < 0) {
      perror("bounded: kill");
    }
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
    }
    return 124;
  }
  if (WIFEXITED(status)) {
    return WEXITSTATUS(status);
  }
  if (WIFSIGNALED(status)) {
    return 128 + WTERMSIG(status);
  }
  return 125;
}
