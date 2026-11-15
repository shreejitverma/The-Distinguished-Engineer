/*
 * prio.c: report the scheduling attributes that nice, renice, and ionice change.
 *
 *   prio self           nice value of the calling process (getpriority(PRIO_PROCESS, 0))
 *   prio can-lower      raise the nice value by one, then try to lower it again;
 *                       prints "lower=allowed" or "lower=denied errno=NAME"
 *   prio threads        delta of the nice value seen by the main thread, by a thread created
 *                       before setpriority(PRIO_PROCESS, getpid(), base + 1), and by one
 *                       created after it
 *   prio rlimit-nice    soft RLIMIT_NICE, or "none" where the platform has no such limit
 *   prio ioprio         Linux only: raw ioprio_get(IOPRIO_WHO_PROCESS, 0) as class and level
 */
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/syscall.h>
#endif

static int get_nice(void) {
  errno = 0;
  int v = getpriority(PRIO_PROCESS, 0);
  if (v == -1 && errno != 0) {
    perror("getpriority");
    exit(2);
  }
  return v;
}

static const char *errno_name(int e) {
  switch (e) {
    case EACCES:
      return "EACCES";
    case EPERM:
      return "EPERM";
    default:
      return "other";
  }
}

static int mode_can_lower(void) {
  int base = get_nice();
  if (setpriority(PRIO_PROCESS, 0, base + 1) != 0) {
    perror("setpriority(raise)");
    return 2;
  }
  int raised = get_nice();
  if (raised == base) {
    fprintf(stderr, "nice value %d is already the maximum\n", base);
    return 2;
  }
  if (setpriority(PRIO_PROCESS, 0, raised - 1) == 0) {
    printf("lower=allowed\n");
  } else {
    printf("lower=denied errno=%s\n", errno_name(errno));
  }
  return 0;
}

struct reporter {
  int fd;      /* read end: one byte means "report now" */
  int result;  /* nice value this thread saw */
};

static void *report_after_signal(void *arg) {
  struct reporter *r = arg;
  char c;
  if (r->fd >= 0 && read(r->fd, &c, 1) != 1) {
    r->result = -100;
    return NULL;
  }
  errno = 0;
  r->result = getpriority(PRIO_PROCESS, 0);
  return NULL;
}

static int mode_threads(void) {
  int fds[2];
  if (pipe(fds) != 0) {
    perror("pipe");
    return 2;
  }
  int base = get_nice();
  struct reporter old_thread = {fds[0], 0};
  struct reporter new_thread = {-1, 0};
  pthread_t t_old, t_new;
  if (pthread_create(&t_old, NULL, report_after_signal, &old_thread) != 0) {
    fprintf(stderr, "pthread_create failed\n");
    return 2;
  }
  /* who = getpid(): the process ID, which on Linux names only the thread-group leader. */
  if (setpriority(PRIO_PROCESS, (id_t)getpid(), base + 1) != 0) {
    perror("setpriority");
    return 2;
  }
  int main_after = get_nice();
  if (main_after == base) {
    fprintf(stderr, "nice value %d is already the maximum\n", base);
    return 2;
  }
  if (pthread_create(&t_new, NULL, report_after_signal, &new_thread) != 0) {
    fprintf(stderr, "pthread_create failed\n");
    return 2;
  }
  ssize_t w = write(fds[1], "x", 1);
  if (w != 1) {
    perror("write");
    return 2;
  }
  pthread_join(t_old, NULL);
  pthread_join(t_new, NULL);
  printf("main=+%d old_thread=+%d new_thread=+%d\n", main_after - base,
         old_thread.result - base, new_thread.result - base);
  return 0;
}

static int mode_rlimit_nice(void) {
#ifdef RLIMIT_NICE
  struct rlimit rl;
  if (getrlimit(RLIMIT_NICE, &rl) != 0) {
    perror("getrlimit");
    return 2;
  }
  if (rl.rlim_cur == RLIM_INFINITY) {
    printf("unlimited\n");
  } else {
    printf("%llu\n", (unsigned long long)rl.rlim_cur);
  }
#else
  printf("none\n");
#endif
  return 0;
}

static int mode_ioprio(void) {
#ifdef __linux__
  /* 1 is IOPRIO_WHO_PROCESS; the class is bits 13..15, the level bits 0..2 (linux/ioprio.h). */
  long v = syscall(SYS_ioprio_get, 1, 0);
  if (v == -1) {
    perror("ioprio_get");
    return 2;
  }
  printf("class=%ld level=%ld\n", (v >> 13) & 7, v & 7);
  return 0;
#else
  fprintf(stderr, "ioprio_get is Linux-only\n");
  return 2;
#endif
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "";
  if (strcmp(mode, "self") == 0) {
    printf("%d\n", get_nice());
    return 0;
  }
  if (strcmp(mode, "can-lower") == 0) return mode_can_lower();
  if (strcmp(mode, "threads") == 0) return mode_threads();
  if (strcmp(mode, "rlimit-nice") == 0) return mode_rlimit_nice();
  if (strcmp(mode, "ioprio") == 0) return mode_ioprio();
  fprintf(stderr, "usage: prio self|can-lower|threads|rlimit-nice|ioprio\n");
  return 2;
}
