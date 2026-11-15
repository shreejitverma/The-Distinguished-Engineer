/*
 * lockprobe: show the kernel semantics of flock(2), process-owned fcntl(2)
 * record locks, and open file description (OFD) fcntl(2) locks.
 *
 * Every mode prints one line per fact, so a test can compare exact output.
 * KIND is one of: flock, fcntl, ofd.
 *
 *   lockprobe owner FILE       two opens in one process: do they conflict?
 *   lockprobe close FILE       does closing an unrelated descriptor drop the lock?
 *   lockprobe fork FILE        does a forked child keep the lock after the parent closes?
 *   lockprobe rdonly FILE      can a read-only descriptor take an exclusive lock?
 *   lockprobe mix FILE         do flock and fcntl locks see each other?
 *   lockprobe getlk FILE       what does F_GETLK report as the owner of each kind?
 *   lockprobe upgrade FILE     does a failed LOCK_SH -> LOCK_EX|LOCK_NB keep the shared lock?
 *   lockprobe deadlock KIND FILE   is an AB-BA wait cycle detected?
 *   lockprobe lockfd FD KIND   lock an inherited descriptor without blocking, then exit
 *   lockprobe test FILE KIND   open FILE anew and report free or busy
 *   lockprobe bench N FILE     time N uncontended lock+unlock pairs per kind
 *
 * Exit status: 0 on success; for lockfd 1 when busy; 2 on a usage or system error.
 */
#define _GNU_SOURCE 1

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

#if defined(__linux__) && !defined(F_OFD_SETLK)
/* Linux 3.15 values, for C libraries that hide them (util-linux flock.c does the same). */
#define F_OFD_GETLK 36
#define F_OFD_SETLK 37
#define F_OFD_SETLKW 38
#endif

enum kind { K_FLOCK, K_FCNTL, K_OFD };
static const char *const kind_names[] = {"flock", "fcntl", "ofd"};

static void die(const char *what) {
  fprintf(stderr, "lockprobe: %s: %s\n", what, strerror(errno));
  exit(2);
}

static const char *errname(int e) {
  switch (e) {
  case 0: return "ok";
  case EAGAIN: return "EAGAIN";
#if EWOULDBLOCK != EAGAIN
  case EWOULDBLOCK: return "EWOULDBLOCK";
#endif
  case EACCES: return "EACCES";
  case EBADF: return "EBADF";
  case EDEADLK: return "EDEADLK";
  case EINTR: return "EINTR";
  case EINVAL: return "EINVAL";
  case ENOLCK: return "ENOLCK";
  default: return "other";
  }
}

static int is_busy(int e) { return e == EAGAIN || e == EWOULDBLOCK || e == EACCES; }

static int parse_kind(const char *s) {
  int k;
  for (k = 0; k < 3; k++)
    if (strcmp(s, kind_names[k]) == 0)
      return k;
  fprintf(stderr, "lockprobe: unknown kind: %s\n", s);
  exit(2);
}

/* Lock bytes [start, start+len) (len 0 = to EOF; flock ignores the range).
 * type is F_WRLCK, F_RDLCK, or F_UNLCK. Returns 0 or an errno value. */
static int range_lock(int fd, int kind, int type, off_t start, off_t len, int wait) {
  int rc;
  if (kind == K_FLOCK) {
    int op = type == F_WRLCK ? LOCK_EX : type == F_RDLCK ? LOCK_SH : LOCK_UN;
    rc = flock(fd, op | (wait ? 0 : LOCK_NB));
  } else {
    struct flock fl;
    int cmd;
    memset(&fl, 0, sizeof fl); /* l_pid must be 0 for the OFD commands */
    fl.l_type = (short)type;
    fl.l_whence = SEEK_SET;
    fl.l_start = start;
    fl.l_len = len;
    if (kind == K_OFD)
      cmd = wait ? F_OFD_SETLKW : F_OFD_SETLK;
    else
      cmd = wait ? F_SETLKW : F_SETLK;
    rc = fcntl(fd, cmd, &fl);
  }
  return rc == 0 ? 0 : errno;
}

static int lock_ex(int fd, int kind) { return range_lock(fd, kind, F_WRLCK, 0, 0, 0); }

static int open_or_die(const char *path, int flags) {
  int fd = open(path, flags);
  if (fd < 0)
    die(path);
  return fd;
}

static void reap(pid_t pid, int *status) {
  while (waitpid(pid, status, 0) < 0)
    if (errno != EINTR)
      die("waitpid");
}

/* A fresh process opens path and tries a non-blocking exclusive lock of kind. */
static const char *tester(const char *path, int kind) {
  int status = 0;
  pid_t pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    int fd = open(path, O_RDWR);
    int e;
    if (fd < 0)
      _exit(3);
    e = lock_ex(fd, kind);
    _exit(e == 0 ? 0 : is_busy(e) ? 1 : 2);
  }
  reap(pid, &status);
  if (!WIFEXITED(status))
    return "killed";
  switch (WEXITSTATUS(status)) {
  case 0: return "free";
  case 1: return "busy";
  default: return "error";
  }
}

/* A child that takes a lock of kind and type on its own open of path, reports
 * readiness, and holds the lock until the parent closes *release. */
static pid_t spawn_holder(const char *path, int kind, int type, int *release) {
  int ready[2], hold[2];
  char c;
  pid_t pid;
  if (pipe(ready) < 0 || pipe(hold) < 0)
    die("pipe");
  pid = fork();
  if (pid < 0)
    die("fork");
  if (pid == 0) {
    int fd = open(path, O_RDWR);
    close(ready[0]);
    close(hold[1]);
    if (fd < 0 || range_lock(fd, kind, type, 0, 0, 0) != 0)
      _exit(2); /* the parent sees EOF on ready without a byte */
    if (write(ready[1], "L", 1) != 1)
      _exit(2);
    while (read(hold[0], &c, 1) > 0)
      ; /* returns 0 (EOF) when the parent closes its end */
    _exit(0);
  }
  close(ready[1]);
  close(hold[0]);
  if (read(ready[0], &c, 1) != 1) {
    fprintf(stderr, "lockprobe: holder failed to lock\n");
    exit(2);
  }
  close(ready[0]);
  *release = hold[1];
  return pid;
}

static void release_holder(pid_t pid, int release) {
  int status;
  close(release);
  reap(pid, &status);
}

static int mode_owner(const char *path) {
  int k, fd1, fd2, e;
  for (k = 0; k < 3; k++) {
    fd1 = open_or_die(path, O_RDWR);
    fd2 = open_or_die(path, O_RDWR);
    if (lock_ex(fd1, k) != 0)
      die("first lock");
    e = lock_ex(fd2, k);
    printf("%s, second open in the same process: %s\n", kind_names[k],
           e == 0 ? "granted" : is_busy(e) ? "busy" : errname(e));
    close(fd2);
    close(fd1);
  }
  fd1 = open_or_die(path, O_RDWR);
  fd2 = open_or_die(path, O_RDWR);
  if (lock_ex(fd1, K_OFD) != 0)
    die("ofd lock");
  e = lock_ex(fd2, K_FCNTL);
  printf("ofd held, fcntl in the same process: %s\n",
         e == 0 ? "granted" : is_busy(e) ? "busy" : errname(e));
  close(fd2);
  close(fd1);
  return 0;
}

static int mode_close(const char *path) {
  int k, fd1, fd2;
  for (k = 0; k < 3; k++) {
    fd1 = open_or_die(path, O_RDWR);
    if (lock_ex(fd1, k) != 0)
      die("lock");
    fd2 = open_or_die(path, O_RDONLY); /* an unrelated open, as a library would do */
    close(fd2);
    printf("%s after closing an unrelated descriptor: %s\n", kind_names[k], tester(path, k));
    close(fd1);
  }
  return 0;
}

static int mode_fork(const char *path) {
  int k, fd, gate[2], status;
  char c;
  pid_t child;
  for (k = 0; k < 3; k++) {
    fd = open_or_die(path, O_RDWR);
    if (lock_ex(fd, k) != 0)
      die("lock");
    if (pipe(gate) < 0)
      die("pipe");
    child = fork();
    if (child < 0)
      die("fork");
    if (child == 0) { /* holds the inherited descriptor until the gate closes */
      close(gate[1]);
      while (read(gate[0], &c, 1) > 0)
        ;
      _exit(0);
    }
    close(gate[0]);
    close(fd); /* the parent lets go; only the child's copy remains */
    printf("%s after the parent closes, child still open: %s\n", kind_names[k], tester(path, k));
    close(gate[1]);
    reap(child, &status);
  }
  return 0;
}

static int mode_rdonly(const char *path) {
  int k, fd, e;
  for (k = 0; k < 3; k++) {
    fd = open_or_die(path, O_RDONLY);
    e = lock_ex(fd, k);
    printf("%s exclusive lock on an O_RDONLY descriptor: %s\n", kind_names[k], errname(e));
    close(fd);
  }
  return 0;
}

static int mode_mix(const char *path) {
  int release;
  pid_t pid = spawn_holder(path, K_FLOCK, F_WRLCK, &release);
  printf("flock held, fcntl tester: %s\n", tester(path, K_FCNTL));
  release_holder(pid, release);
  pid = spawn_holder(path, K_FCNTL, F_WRLCK, &release);
  printf("fcntl held, flock tester: %s\n", tester(path, K_FLOCK));
  release_holder(pid, release);
  pid = spawn_holder(path, K_OFD, F_WRLCK, &release);
  printf("ofd held, fcntl tester: %s\n", tester(path, K_FCNTL));
  release_holder(pid, release);
  return 0;
}

static int mode_getlk(const char *path) {
  int k, release, fd;
  for (k = 0; k < 3; k++) {
    struct flock fl;
    pid_t pid = spawn_holder(path, k, F_WRLCK, &release);
    fd = open_or_die(path, O_RDWR);
    memset(&fl, 0, sizeof fl);
    fl.l_type = F_WRLCK;
    fl.l_whence = SEEK_SET;
    if (fcntl(fd, F_GETLK, &fl) < 0)
      die("F_GETLK");
    if (fl.l_type == F_UNLCK)
      printf("F_GETLK on a %s lock: no conflict reported\n", kind_names[k]);
    else if (fl.l_pid == pid)
      printf("F_GETLK on a %s lock: l_pid is the holder\n", kind_names[k]);
    else
      printf("F_GETLK on a %s lock: l_pid %ld\n", kind_names[k], (long)fl.l_pid);
    close(fd);
    release_holder(pid, release);
  }
  return 0;
}

static int mode_upgrade(const char *path) {
  int release, e;
  int fd = open_or_die(path, O_RDWR);
  pid_t pid;
  if (range_lock(fd, K_FLOCK, F_RDLCK, 0, 0, 0) != 0)
    die("shared lock");
  pid = spawn_holder(path, K_FLOCK, F_RDLCK, &release); /* a second reader */
  e = flock(fd, LOCK_EX | LOCK_NB) == 0 ? 0 : errno;
  printf("upgrade while another process shares: %s\n", is_busy(e) ? "busy" : errname(e));
  release_holder(pid, release);
  /* If our shared lock survived the failed upgrade, a writer is still refused. */
  printf("shared lock after the failed upgrade: %s\n",
         strcmp(tester(path, K_FLOCK), "busy") == 0 ? "kept" : "lost");
  close(fd);
  return 0;
}

static void on_alarm(int sig) { (void)sig; }

static int mode_deadlock(int kind, const char *path) {
  /* flock locks whole files, so the cycle uses two files; fcntl and ofd use bytes 0 and 1. */
  char path2[4096];
  int a1, a2, ready[2], result[2], ea, n, status;
  char c, eb[32];
  struct sigaction sa;
  pid_t b;

  if (snprintf(path2, sizeof path2, "%s.2", path) >= (int)sizeof path2)
    die("path too long");
  if (kind == K_FLOCK) {
    a2 = open(path2, O_RDWR | O_CREAT, 0644);
    if (a2 < 0)
      die(path2);
    close(a2);
  }
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = on_alarm; /* no SA_RESTART: a blocked lock call returns EINTR */
  sigemptyset(&sa.sa_mask);
  if (sigaction(SIGALRM, &sa, NULL) < 0)
    die("sigaction");

  a1 = open_or_die(path, O_RDWR);
  if (range_lock(a1, kind, F_WRLCK, 0, 1, 0) != 0)
    die("A first lock");
  if (pipe(ready) < 0 || pipe(result) < 0)
    die("pipe");
  b = fork();
  if (b < 0)
    die("fork");
  if (b == 0) {
    int b1, b2, e;
    const char *name;
    close(a1);
    close(ready[0]);
    close(result[0]);
    b1 = open(path, O_RDWR);
    b2 = kind == K_FLOCK ? open(path2, O_RDWR) : open(path, O_RDWR);
    if (b1 < 0 || b2 < 0)
      _exit(2);
    if (kind == K_FLOCK ? range_lock(b2, kind, F_WRLCK, 0, 0, 0) : range_lock(b2, kind, F_WRLCK, 1, 1, 0))
      _exit(2);
    if (write(ready[1], "r", 1) != 1)
      _exit(2);
    sleep(1); /* let A block on the byte (or file) B holds */
    alarm(3);
    e = range_lock(b1, kind, F_WRLCK, 0, 1, 1);
    alarm(0);
    name = errname(e);
    if (write(result[1], name, strlen(name)) < 0) {
      /* the parent reads "" and reports it; nothing else to do here */
    }
    _exit(0);
  }
  close(ready[1]);
  close(result[1]);
  if (read(ready[0], &c, 1) != 1) {
    fprintf(stderr, "lockprobe: B failed to lock\n");
    exit(2);
  }
  close(ready[0]);
  alarm(10); /* bound A even if B misbehaves */
  if (kind == K_FLOCK) {
    a2 = open_or_die(path2, O_RDWR);
    ea = range_lock(a2, kind, F_WRLCK, 0, 0, 1);
  } else {
    a2 = -1;
    ea = range_lock(a1, kind, F_WRLCK, 1, 1, 1);
  }
  alarm(0);
  close(a1); /* lets B through if A was the one refused */
  if (a2 >= 0)
    close(a2);
  n = (int)read(result[0], eb, sizeof eb - 1);
  eb[n > 0 ? n : 0] = '\0';
  close(result[0]);
  reap(b, &status);
  if (ea == EDEADLK || strcmp(eb, "EDEADLK") == 0)
    printf("%s AB-BA cycle: EDEADLK\n", kind_names[kind]);
  else if (strcmp(eb, "EINTR") == 0)
    printf("%s AB-BA cycle: undetected, B waited until its 3 s alarm (EINTR)\n", kind_names[kind]);
  else
    printf("%s AB-BA cycle: unexpected (A %s, B %s)\n", kind_names[kind], errname(ea), eb);
  return 0;
}

static int mode_lockfd(const char *fdstr, int kind) {
  char *end;
  long fd = strtol(fdstr, &end, 10);
  int e;
  if (*fdstr == '\0' || *end != '\0' || fd < 0 || fd > 1024) {
    fprintf(stderr, "lockprobe: bad descriptor: %s\n", fdstr);
    return 2;
  }
  e = lock_ex((int)fd, kind);
  if (e == 0) {
    puts("locked");
    return 0;
  }
  puts(is_busy(e) ? "busy" : errname(e));
  return is_busy(e) ? 1 : 2;
}

static long long now_ns(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
    die("clock_gettime");
  return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static int mode_bench(const char *nstr, const char *path) {
  long n = strtol(nstr, NULL, 10), i;
  int k, fd;
  long long t0, t1;
  if (n <= 0) {
    fprintf(stderr, "lockprobe: bad count: %s\n", nstr);
    return 2;
  }
  for (k = 0; k < 3; k++) {
    fd = open_or_die(path, O_RDWR);
    t0 = now_ns();
    for (i = 0; i < n; i++)
      if (range_lock(fd, k, F_WRLCK, 0, 0, 0) != 0 || range_lock(fd, k, F_UNLCK, 0, 0, 0) != 0)
        die("bench lock");
    t1 = now_ns();
    printf("%s: %lld ns per lock+unlock pair (n=%ld)\n", kind_names[k], (t1 - t0) / n, n);
    close(fd);
  }
  return 0;
}

int main(int argc, char **argv) {
  setvbuf(stdout, NULL, _IONBF, 0); /* no buffered output duplicated into children */
  if (argc == 3 && strcmp(argv[1], "owner") == 0)
    return mode_owner(argv[2]);
  if (argc == 3 && strcmp(argv[1], "close") == 0)
    return mode_close(argv[2]);
  if (argc == 3 && strcmp(argv[1], "fork") == 0)
    return mode_fork(argv[2]);
  if (argc == 3 && strcmp(argv[1], "rdonly") == 0)
    return mode_rdonly(argv[2]);
  if (argc == 3 && strcmp(argv[1], "mix") == 0)
    return mode_mix(argv[2]);
  if (argc == 3 && strcmp(argv[1], "getlk") == 0)
    return mode_getlk(argv[2]);
  if (argc == 3 && strcmp(argv[1], "upgrade") == 0)
    return mode_upgrade(argv[2]);
  if (argc == 4 && strcmp(argv[1], "deadlock") == 0)
    return mode_deadlock(parse_kind(argv[2]), argv[3]);
  if (argc == 4 && strcmp(argv[1], "lockfd") == 0)
    return mode_lockfd(argv[2], parse_kind(argv[3]));
  if (argc == 4 && strcmp(argv[1], "test") == 0) {
    puts(tester(argv[2], parse_kind(argv[3])));
    return 0;
  }
  if (argc == 4 && strcmp(argv[1], "bench") == 0)
    return mode_bench(argv[2], argv[3]);
  fprintf(stderr, "usage: lockprobe owner|close|fork|rdonly|mix|getlk|upgrade FILE\n"
                  "       lockprobe deadlock KIND FILE | lockfd FD KIND | test FILE KIND | bench N FILE\n");
  return 2;
}
