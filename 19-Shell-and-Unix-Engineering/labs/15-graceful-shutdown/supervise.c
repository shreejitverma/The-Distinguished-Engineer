/*
 * supervise: a stand-in for the supervisor above a service (systemd, docker stop, the kubelet).
 * It starts CMD in its own process group, waits for the READY file, lets CMD settle for one
 * second, sends the stop signal, gives CMD a grace period, then escalates to SIGKILL for the
 * whole group, and finally reports what the kernel's wait status says.
 *
 * Usage: supervise [-G] [-t SIG] [-n COUNT] [-i SECONDS] [-w GRACE] -r READY -- CMD [ARG...]
 *   -r READY    file CMD creates once its traps are installed and its first job runs
 *   -t SIG      stop signal (default TERM); HUP, INT, QUIT, TERM, USR1, USR2
 *   -n COUNT    send the stop signal COUNT times (default 1)
 *   -i SECONDS  seconds between repeated stop signals (default 1)
 *   -w GRACE    seconds after the first stop signal before SIGKILL (default 20)
 *   -G          send the stop signal to the whole process group, like systemd KillMode=control-group;
 *               without -G it goes to CMD alone, like docker stop and the kubelet (PID 1 of the container)
 *
 * Output, one field per line: "exited N" or "signaled NAME" (the kernel's view, which a shell's $?
 * cannot give), "escalated yes|no" (SIGKILL was needed), "stragglers yes|no" (a process of the
 * group was still alive when the leader was reaped), "sent N" (stop signals actually sent), and
 * "elapsed_ms N" (first stop signal until the leader was reaped).
 * The child starts with HUP, INT, QUIT, TERM, and PIPE at their default disposition, an empty signal
 * mask, and RLIMIT_CORE 0. Every wait is bounded: 10 s for READY, grace plus 5 s for the child.
 * Nothing outlives the run: the whole group gets SIGKILL before supervise exits.
 */

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const struct {
  const char *name;
  int num;
} names[] = {
    {"HUP", SIGHUP},   {"INT", SIGINT},   {"QUIT", SIGQUIT}, {"KILL", SIGKILL},
    {"PIPE", SIGPIPE}, {"TERM", SIGTERM}, {"USR1", SIGUSR1}, {"USR2", SIGUSR2},
};

static int sig_from_name(const char *s) {
  size_t i;
  for (i = 0; i < sizeof names / sizeof names[0]; i++)
    if (strcmp(s, names[i].name) == 0)
      return names[i].num;
  return -1;
}

static void print_sig(int sig) {
  size_t i;
  for (i = 0; i < sizeof names / sizeof names[0]; i++)
    if (names[i].num == sig) {
      printf("signaled %s\n", names[i].name);
      return;
    }
  printf("signaled %d\n", sig);
}

static long now_ms(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
    return 0;
  return (long)ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void nap_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  while (nanosleep(&ts, &ts) < 0 && errno == EINTR) {
  }
}

static int usage(void) {
  fprintf(stderr, "usage: supervise [-G] [-t SIG] [-n COUNT] [-i SECONDS] [-w GRACE] -r READY -- CMD [ARG...]\n");
  return 2;
}

int main(int argc, char **argv) {
  int to_group = 0, stop_sig = SIGTERM, count = 1, interval = 1, grace = 20, opt;
  const char *ready = NULL;
  pid_t pid, target;
  int status = 0, reaped = 0, sent = 0, escalated = 0;
  long tick, t0, next_send, deadline, hard, elapsed;
  struct stat st;

  while ((opt = getopt(argc, argv, "Gt:n:i:w:r:")) != -1) {
    switch (opt) {
    case 'G':
      to_group = 1;
      break;
    case 't':
      if ((stop_sig = sig_from_name(optarg)) < 0)
        return usage();
      break;
    case 'n':
      count = atoi(optarg);
      break;
    case 'i':
      interval = atoi(optarg);
      break;
    case 'w':
      grace = atoi(optarg);
      break;
    case 'r':
      ready = optarg;
      break;
    default:
      return usage();
    }
  }
  if (optind >= argc || ready == NULL || count < 1 || interval < 0 || grace < 1)
    return usage();

  fflush(stdout);
  pid = fork();
  if (pid < 0) {
    perror("supervise: fork");
    return 1;
  }
  if (pid == 0) {
    static const int dfl[] = {SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGPIPE};
    struct rlimit rl;
    sigset_t none;
    size_t i;
    for (i = 0; i < sizeof dfl / sizeof dfl[0]; i++)
      signal(dfl[i], SIG_DFL);
    sigemptyset(&none);
    sigprocmask(SIG_SETMASK, &none, NULL);
    rl.rlim_cur = rl.rlim_max = 0;
    if (setrlimit(RLIMIT_CORE, &rl) < 0)
      perror("supervise: setrlimit");
    if (setpgid(0, 0) < 0)
      perror("supervise: setpgid");
    execvp(argv[optind], argv + optind);
    perror("supervise: exec");
    _exit(127);
  }
  setpgid(pid, pid); /* close the race with the child's own setpgid */
  target = to_group ? -pid : pid;

  for (tick = 0; tick < 100 && stat(ready, &st) < 0; tick++) {
    if (waitpid(pid, &status, WNOHANG) == pid) {
      reaped = 1;
      break;
    }
    nap_ms(100);
  }
  if (!reaped && tick == 100) {
    kill(-pid, SIGKILL);
    waitpid(pid, &status, 0);
    printf("timeout waiting for ready\n");
    return 1;
  }
  if (!reaped)
    nap_ms(1000); /* let the command settle: a signal between fork and exec is lost in some shells */

  t0 = now_ms();
  next_send = t0;
  deadline = t0 + grace * 1000L;
  hard = deadline + 5000L;
  while (!reaped) {
    long t = now_ms();
    pid_t r = waitpid(pid, &status, WNOHANG);
    if (r == pid) {
      reaped = 1;
      break;
    }
    if (r < 0) {
      perror("supervise: waitpid");
      kill(-pid, SIGKILL);
      return 1;
    }
    if (sent < count && t >= next_send) {
      if (kill(target, stop_sig) < 0)
        perror("supervise: kill");
      sent++;
      next_send = t + interval * 1000L;
    }
    if (!escalated && t >= deadline) {
      kill(-pid, SIGKILL); /* container runtimes and systemd kill every remaining process */
      escalated = 1;
    }
    if (t >= hard) {
      kill(-pid, SIGKILL);
      waitpid(pid, &status, 0);
      printf("timeout\n");
      return 1;
    }
    nap_ms(20);
  }
  elapsed = now_ms() - t0;

  if (WIFEXITED(status))
    printf("exited %d\n", WEXITSTATUS(status));
  else if (WIFSIGNALED(status))
    print_sig(WTERMSIG(status));
  else
    printf("other %d\n", status);
  printf("escalated %s\n", escalated ? "yes" : "no");
  printf("stragglers %s\n", kill(-pid, 0) == 0 ? "yes" : "no");
  kill(-pid, SIGKILL); /* nothing may outlive the run */
  printf("sent %d\n", sent);
  printf("elapsed_ms %ld\n", elapsed);
  return 0;
}
