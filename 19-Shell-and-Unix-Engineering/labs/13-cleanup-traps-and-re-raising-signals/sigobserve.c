/*
 * sigobserve: run a command, optionally signal it once it reports ready, and
 * print how it ended as the kernel's wait status says: "exited N" when
 * WIFEXITED, "signaled NAME" when WIFSIGNALED. A shell's $? cannot tell the
 * two apart (exit 143 and death by SIGTERM both read 143 in bash), which is
 * the whole point of re-raising a signal, so the lab observes it from C.
 *
 * Usage: sigobserve [-g] [-G] [-i SIG] [-k SIG -r READY] -- CMD [ARG...]
 *   -i SIG    start CMD with SIG ignored (as an asynchronous list would)
 *   -k SIG    send SIG once READY exists, then wait one more second first
 *   -r READY  file CMD creates when its traps are installed
 *   -g        make CMD the leader of a new process group
 *   -G        send the -k signal to CMD's whole process group (needs -g)
 *
 * The child starts with HUP, INT, QUIT, TERM, and PIPE at their default
 * disposition (unless -i), an empty signal mask, and RLIMIT_CORE 0, so the
 * result does not depend on what the test harness inherited.
 * Everything is bounded: 10 s for READY, 30 s for the child to end; on a
 * timeout the child (or its group) gets SIGKILL and the status is "timeout".
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
    {"HUP", SIGHUP},   {"INT", SIGINT},   {"QUIT", SIGQUIT},
    {"KILL", SIGKILL}, {"PIPE", SIGPIPE}, {"TERM", SIGTERM},
    {"USR1", SIGUSR1}, {"USR2", SIGUSR2},
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

static void nap_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  while (nanosleep(&ts, &ts) < 0 && errno == EINTR) {
  }
}

static int usage(void) {
  fprintf(stderr, "usage: sigobserve [-g] [-G] [-i SIG] [-k SIG -r READY] -- CMD [ARG...]\n");
  return 2;
}

int main(int argc, char **argv) {
  int group = 0, to_group = 0, ignore_sig = 0, send_sig = 0, opt;
  const char *ready = NULL;
  pid_t pid, target;
  int status = 0, waited = 0;
  long tick;
  struct stat st;

  while ((opt = getopt(argc, argv, "gGi:k:r:")) != -1) {
    switch (opt) {
    case 'g':
      group = 1;
      break;
    case 'G':
      to_group = 1;
      break;
    case 'i':
      if ((ignore_sig = sig_from_name(optarg)) < 0)
        return usage();
      break;
    case 'k':
      if ((send_sig = sig_from_name(optarg)) < 0)
        return usage();
      break;
    case 'r':
      ready = optarg;
      break;
    default:
      return usage();
    }
  }
  if (optind >= argc || (send_sig && !ready) || (to_group && !group))
    return usage();

  fflush(stdout);
  pid = fork();
  if (pid < 0) {
    perror("sigobserve: fork");
    return 1;
  }
  if (pid == 0) {
    static const int dfl[] = {SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGPIPE};
    struct rlimit rl;
    sigset_t none;
    size_t i;
    for (i = 0; i < sizeof dfl / sizeof dfl[0]; i++)
      signal(dfl[i], SIG_DFL);
    if (ignore_sig)
      signal(ignore_sig, SIG_IGN);
    sigemptyset(&none);
    sigprocmask(SIG_SETMASK, &none, NULL);
    rl.rlim_cur = rl.rlim_max = 0;
    if (setrlimit(RLIMIT_CORE, &rl) < 0)
      perror("sigobserve: setrlimit");
    if (group && setpgid(0, 0) < 0)
      perror("sigobserve: setpgid");
    execvp(argv[optind], argv + optind);
    perror("sigobserve: exec");
    _exit(127);
  }
  if (group)
    setpgid(pid, pid); /* close the race with the child's own setpgid */
  target = to_group ? -pid : pid;

  if (send_sig) {
    for (tick = 0; tick < 100 && stat(ready, &st) < 0; tick++) {
      if (waitpid(pid, &status, WNOHANG) == pid) {
        waited = 1;
        break;
      }
      nap_ms(100);
    }
    if (!waited && tick == 100) {
      kill(target, SIGKILL);
      waitpid(pid, &status, 0);
      printf("timeout\n");
      return 1;
    }
    if (!waited) {
      nap_ms(1000); /* let the command reach its wait */
      if (kill(target, send_sig) < 0)
        perror("sigobserve: kill");
    }
  }

  for (tick = 0; !waited && tick < 300; tick++) {
    pid_t r = waitpid(pid, &status, WNOHANG);
    if (r == pid)
      waited = 1;
    else if (r < 0) {
      perror("sigobserve: waitpid");
      return 1;
    } else
      nap_ms(100);
  }
  if (!waited) {
    kill(target, SIGKILL);
    waitpid(pid, &status, 0);
    printf("timeout\n");
    return 1;
  }
  if (to_group)
    kill(-pid, SIGKILL); /* nothing may outlive the test */

  if (WIFEXITED(status))
    printf("exited %d\n", WEXITSTATUS(status));
  else if (WIFSIGNALED(status))
    print_sig(WTERMSIG(status));
  else
    printf("other %d\n", status);
  return 0;
}
