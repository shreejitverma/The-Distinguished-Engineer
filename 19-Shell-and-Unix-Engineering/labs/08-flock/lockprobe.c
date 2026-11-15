/*
 * lockprobe.c - show who owns a flock(2) lock and who owns an fcntl(2) lock.
 *
 * Usage: lockprobe MODE PATH [N]
 *   two-opens    two open() calls in one process: do their flock locks conflict?
 *   dup          a dup()ed descriptor shares the flock lock and can release it
 *   posix-close  closing an unrelated descriptor drops fcntl locks, not flock locks
 *   inherit      fork(): the child shares a flock lock but does not own fcntl locks
 *   mix          does a flock lock block an fcntl lock on the same file?
 *   ofd          classic fcntl locks are per process; F_OFD_SETLK locks are per open
 *   deadlock     F_SETLKW reports EDEADLK; flock() just blocks
 *   bench        N uncontended lock+unlock pairs for flock() and F_SETLK
 *
 * Every wait is bounded with alarm(), so no mode can hang.
 */
#define _GNU_SOURCE /* F_OFD_* in glibc */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void die(const char *what) {
  perror(what);
  exit(2);
}

static int open_rw(const char *path) {
  int fd = open(path, O_RDWR | O_CREAT, 0644);
  if (fd < 0)
    die("open");
  return fd;
}

/* Try a whole-file fcntl lock with command cmd; return 0 or the errno. */
static int posix_lock(int fd, int cmd, short type) {
  struct flock fl;
  memset(&fl, 0, sizeof fl);
  fl.l_type = type;
  fl.l_whence = SEEK_SET;
  fl.l_start = 0;
  fl.l_len = 0;
  return fcntl(fd, cmd, &fl) == 0 ? 0 : errno;
}

static int posix_byte(int fd, int cmd, off_t byte) {
  struct flock fl;
  memset(&fl, 0, sizeof fl);
  fl.l_type = F_WRLCK;
  fl.l_whence = SEEK_SET;
  fl.l_start = byte;
  fl.l_len = 1;
  return fcntl(fd, cmd, &fl) == 0 ? 0 : errno;
}

static const char *verdict(int err) {
  if (err == 0)
    return "granted";
  if (err == EWOULDBLOCK || err == EAGAIN || err == EACCES)
    return "blocked";
  if (err == EDEADLK)
    return "EDEADLK";
  if (err == EINTR)
    return "EINTR";
  return strerror(err);
}

static int try_flock(int fd, int op) {
  return flock(fd, op) == 0 ? 0 : errno;
}

static void on_alarm(int sig) {
  (void)sig;
}

/* SIGALRM interrupts blocking calls: no SA_RESTART. */
static void arm_alarm(unsigned seconds) {
  struct sigaction sa;
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = on_alarm;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  if (sigaction(SIGALRM, &sa, NULL) != 0)
    die("sigaction");
  alarm(seconds);
}

static void wait_child(pid_t pid) {
  int status;
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR)
      die("waitpid");
  }
}

static void block_on_pipe(int rfd) {
  char c;
  ssize_t n = read(rfd, &c, 1);
  if (n < 0) {
    /* EINTR from the safety alarm or EOF: either way, stop waiting */
  }
}

static void signal_pipe(int wfd) {
  if (write(wfd, "x", 1) != 1)
    die("write");
}

static void mode_two_opens(const char *path) {
  int a = open_rw(path);
  int b = open_rw(path);
  printf("first-open LOCK_EX: %s\n", verdict(try_flock(a, LOCK_EX | LOCK_NB)));
  printf("second-open LOCK_EX: %s\n", verdict(try_flock(b, LOCK_EX | LOCK_NB)));
}

static void mode_dup(const char *path) {
  int a = open_rw(path);
  int b = dup(a);
  int c = open_rw(path);
  if (b < 0)
    die("dup");
  printf("original LOCK_EX: %s\n", verdict(try_flock(a, LOCK_EX | LOCK_NB)));
  printf("dup LOCK_EX: %s\n", verdict(try_flock(b, LOCK_EX | LOCK_NB)));
  printf("other open while held: %s\n", verdict(try_flock(c, LOCK_EX | LOCK_NB)));
  if (flock(b, LOCK_UN) != 0)
    die("flock LOCK_UN");
  printf("other open after LOCK_UN via dup: %s\n",
         verdict(try_flock(c, LOCK_EX | LOCK_NB)));
}

/* A "library" opens and closes the locked file through a new descriptor. */
static void open_and_close(const char *path) {
  int other = open(path, O_RDONLY);
  if (other < 0)
    die("open other");
  close(other);
}

/* Each lock type gets its own file: on BSD and macOS the two types conflict. */
static void mode_posix_close(const char *path) {
  pid_t pid;
  char fpath[4096];
  int pfd, ffd;
  if (snprintf(fpath, sizeof fpath, "%s.flock", path) >= (int)sizeof fpath)
    die("path too long");
  pfd = open_rw(path);
  ffd = open_rw(fpath);
  if (posix_lock(pfd, F_SETLK, F_WRLCK) != 0)
    die("F_SETLK");
  if (flock(ffd, LOCK_EX | LOCK_NB) != 0)
    die("flock");
  open_and_close(path);
  open_and_close(fpath);
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    int c = open_rw(path);
    int f = open_rw(fpath);
    printf("fcntl lock after closing another fd: %s\n",
           posix_lock(c, F_SETLK, F_WRLCK) == 0 ? "gone" : "held");
    printf("flock lock after closing another fd: %s\n",
           try_flock(f, LOCK_EX | LOCK_NB) == 0 ? "gone" : "held");
    fflush(stdout);
    _exit(0);
  }
  wait_child(pid);
}

static void mode_inherit(const char *path) {
  int sync[2];
  pid_t pid;
  int fd = open_rw(path);
  int probe;
  if (flock(fd, LOCK_EX) != 0)
    die("flock");
  if (pipe(sync) != 0)
    die("pipe");
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    close(sync[1]);
    arm_alarm(10);
    block_on_pipe(sync[0]); /* keep the inherited descriptor open */
    _exit(0);
  }
  close(sync[0]);
  close(fd); /* the parent lets go of its descriptor */
  probe = open_rw(path);
  printf("flock after parent close, child alive: %s\n",
         verdict(try_flock(probe, LOCK_EX | LOCK_NB)));
  signal_pipe(sync[1]);
  wait_child(pid);
  printf("flock after child exit: %s\n", verdict(try_flock(probe, LOCK_EX | LOCK_NB)));
  close(probe);
  close(sync[1]);

  /* fcntl locks are owned by the process: the child does not own them. */
  fd = open_rw(path);
  if (posix_lock(fd, F_SETLK, F_WRLCK) != 0)
    die("F_SETLK");
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    printf("fcntl F_SETLK by child on inherited fd: %s\n",
           verdict(posix_lock(fd, F_SETLK, F_WRLCK)));
    fflush(stdout);
    _exit(0);
  }
  wait_child(pid);
}

static void mode_mix(const char *path) {
  pid_t pid;
  int fd = open_rw(path);
  if (flock(fd, LOCK_EX) != 0)
    die("flock");
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    int c = open_rw(path);
    int err = posix_lock(c, F_SETLK, F_WRLCK);
    printf("fcntl F_SETLK while another process holds flock LOCK_EX: %s\n", verdict(err));
    fflush(stdout);
    _exit(0);
  }
  wait_child(pid);
  if (flock(fd, LOCK_UN) != 0)
    die("flock LOCK_UN");
  /* Same process: an fcntl lock on one open, then flock() on another open. */
  if (posix_lock(fd, F_SETLK, F_WRLCK) != 0)
    die("F_SETLK");
  {
    int other = open_rw(path);
    printf("flock LOCK_NB by the process that holds the fcntl lock: %s\n",
           verdict(try_flock(other, LOCK_EX | LOCK_NB)));
  }
}

static void mode_ofd(const char *path) {
  int a = open_rw(path);
  int b = open_rw(path);
  if (posix_lock(a, F_SETLK, F_WRLCK) != 0)
    die("F_SETLK");
  printf("F_SETLK via second open, same process: %s\n",
         verdict(posix_lock(b, F_SETLK, F_WRLCK)));
  if (posix_lock(a, F_SETLK, F_UNLCK) != 0)
    die("F_UNLCK");
#ifdef F_OFD_SETLK
  {
    int first = posix_lock(a, F_OFD_SETLK, F_WRLCK);
    if (first != 0) {
      printf("F_OFD_SETLK: %s\n", strerror(first));
      return;
    }
    printf("F_OFD_SETLK via second open, same process: %s\n",
           verdict(posix_lock(b, F_OFD_SETLK, F_WRLCK)));
  }
#else
  printf("F_OFD_SETLK: not defined\n");
#endif
}

static void mode_deadlock(const char *path) {
  /* fcntl: A holds byte 0, B holds byte 1, A waits for 1, B asks for 0. */
  int a2b[2], b2a[2];
  pid_t pid;
  int fd;
  char path2[4096];
  if (pipe(a2b) != 0 || pipe(b2a) != 0)
    die("pipe");
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) { /* A */
    int afd = open_rw(path);
    arm_alarm(10);
    if (posix_byte(afd, F_SETLK, 0) != 0)
      die("A F_SETLK 0");
    signal_pipe(a2b[1]);
    block_on_pipe(b2a[0]); /* B holds byte 1 */
    (void)posix_byte(afd, F_SETLKW, 1); /* blocks until B gives up */
    _exit(0);
  }
  fd = open_rw(path); /* B is the parent */
  block_on_pipe(a2b[0]);
  if (posix_byte(fd, F_SETLK, 1) != 0)
    die("B F_SETLK 1");
  signal_pipe(b2a[1]);
  sleep(1); /* margin: let A block in F_SETLKW */
  arm_alarm(5);
  printf("fcntl F_SETLKW closing the cycle: %s\n", verdict(posix_byte(fd, F_SETLKW, 0)));
  alarm(0);
  close(fd); /* releases byte 1, so A finishes */
  wait_child(pid);

  /* flock: the same cycle over two files is never detected. */
  if (snprintf(path2, sizeof path2, "%s.second", path) >= (int)sizeof path2)
    die("path too long");
  fflush(stdout);
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) { /* A */
    int f1 = open_rw(path), f2 = open_rw(path2);
    arm_alarm(10);
    if (flock(f1, LOCK_EX) != 0)
      die("A flock 1");
    signal_pipe(a2b[1]);
    block_on_pipe(b2a[0]);
    (void)flock(f2, LOCK_EX);
    _exit(0);
  }
  {
    int f1 = open_rw(path), f2 = open_rw(path2);
    block_on_pipe(a2b[0]);
    if (flock(f2, LOCK_EX) != 0)
      die("B flock 2");
    signal_pipe(b2a[1]);
    sleep(1);
    arm_alarm(2);
    printf("flock LOCK_EX closing the cycle: %s after 2s alarm\n",
           verdict(try_flock(f1, LOCK_EX)));
    alarm(0);
    close(f2);
    close(f1);
  }
  wait_child(pid);
}

static double elapsed_ns(const struct timespec *t0, const struct timespec *t1) {
  return (double)(t1->tv_sec - t0->tv_sec) * 1e9 + (double)(t1->tv_nsec - t0->tv_nsec);
}

static void mode_bench(const char *path, long n) {
  struct timespec t0, t1;
  int fd = open_rw(path);
  long i;
  clock_gettime(CLOCK_MONOTONIC, &t0);
  for (i = 0; i < n; i++) {
    if (flock(fd, LOCK_EX) != 0 || flock(fd, LOCK_UN) != 0)
      die("flock");
  }
  clock_gettime(CLOCK_MONOTONIC, &t1);
  printf("flock pair ns: %.0f\n", elapsed_ns(&t0, &t1) / (double)n);
  clock_gettime(CLOCK_MONOTONIC, &t0);
  for (i = 0; i < n; i++) {
    if (posix_lock(fd, F_SETLK, F_WRLCK) != 0 || posix_lock(fd, F_SETLK, F_UNLCK) != 0)
      die("fcntl");
  }
  clock_gettime(CLOCK_MONOTONIC, &t1);
  printf("fcntl pair ns: %.0f\n", elapsed_ns(&t0, &t1) / (double)n);
}

int main(int argc, char **argv) {
  const char *mode, *path;
  if (argc < 3) {
    fprintf(stderr, "usage: lockprobe MODE PATH [N]\n");
    return 2;
  }
  mode = argv[1];
  path = argv[2];
  if (strcmp(mode, "two-opens") == 0)
    mode_two_opens(path);
  else if (strcmp(mode, "dup") == 0)
    mode_dup(path);
  else if (strcmp(mode, "posix-close") == 0)
    mode_posix_close(path);
  else if (strcmp(mode, "inherit") == 0)
    mode_inherit(path);
  else if (strcmp(mode, "mix") == 0)
    mode_mix(path);
  else if (strcmp(mode, "ofd") == 0)
    mode_ofd(path);
  else if (strcmp(mode, "deadlock") == 0)
    mode_deadlock(path);
  else if (strcmp(mode, "bench") == 0 && argc == 4)
    mode_bench(path, strtol(argv[3], NULL, 10));
  else {
    fprintf(stderr, "lockprobe: unknown mode %s\n", mode);
    return 2;
  }
  return 0;
}
