/*
 * fsprobe.c - probes for rename(2) and the fsync(2) family, for the
 * "Atomic Rename and fsync" lab.
 *
 * Build: "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -o fsprobe fsprobe.c
 *
 * Modes (each prints one line of key=value pairs and exits 0 unless noted):
 *   features              which flush primitives this build has
 *   dirfd DIR             open a directory O_WRONLY (must fail) and O_RDONLY + fsync
 *   rdonly FILE           fsync and fdatasync through a read-only descriptor
 *   pipe                  fsync and fdatasync on a pipe
 *   samefile DIR          rename() between two hard links of one file
 *   replace DIR NAME TEXT METHOD
 *                         durable replace: mkstemp in DIR, write TEXT, flush
 *                         with METHOD (none, fdatasync, fsync, full), rename
 *                         over DIR/NAME, fsync DIR; exit 2 if METHOD is
 *                         unsupported here, 1 on any failure
 *   bench DIR N           N timed iterations per flush method (microseconds)
 *   fsync [-d|-f] PATH... a tiny fsync(1): open each PATH read-only and call
 *                         fsync (-d fdatasync, -f F_FULLFSYNC); exit 1 if any
 *                         PATH fails, 2 on a usage error or unsupported -f
 * Invoked through a link named "fsync", the program behaves as "fsprobe fsync".
 */
#define _XOPEN_SOURCE 700
#ifdef __APPLE__
#define _DARWIN_C_SOURCE /* F_FULLFSYNC and F_BARRIERFSYNC are Darwin extensions */
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifdef __APPLE__
/* libSystem exports fdatasync, but the macOS SDK's <unistd.h> does not declare it. */
extern int fdatasync(int);
#endif

static const char *errname(int e) {
  static char buf[32];
  switch (e) {
    case 0: return "ok";
    case EACCES: return "EACCES";
    case EBADF: return "EBADF";
    case EEXIST: return "EEXIST";
    case EINVAL: return "EINVAL";
    case EIO: return "EIO";
    case EISDIR: return "EISDIR";
    case ENOENT: return "ENOENT";
    case ENOTDIR: return "ENOTDIR";
    case ENOTSUP: return "ENOTSUP";
    case EROFS: return "EROFS";
    case EXDEV: return "EXDEV";
    default:
      snprintf(buf, sizeof buf, "errno%d", e);
      return buf;
  }
}

/* status_of: "ok" for 0, else the errno name of the failed call. */
static const char *status_of(int rc) { return rc == 0 ? "ok" : errname(errno); }

static int has_full_fsync(void) {
#ifdef F_FULLFSYNC
  return 1;
#else
  return 0;
#endif
}

static int has_barrier_fsync(void) {
#ifdef F_BARRIERFSYNC
  return 1;
#else
  return 0;
#endif
}

/* flush_fd: apply one flush method; returns 0, -1 with errno, or -2 if unsupported here. */
static int flush_fd(int fd, const char *method) {
  if (strcmp(method, "none") == 0) return 0;
  if (strcmp(method, "fsync") == 0) return fsync(fd);
  if (strcmp(method, "fdatasync") == 0) return fdatasync(fd);
  if (strcmp(method, "full") == 0) {
#ifdef F_FULLFSYNC
    return fcntl(fd, F_FULLFSYNC) == -1 ? -1 : 0;
#else
    return -2;
#endif
  }
  if (strcmp(method, "barrier") == 0) {
#ifdef F_BARRIERFSYNC
    return fcntl(fd, F_BARRIERFSYNC) == -1 ? -1 : 0;
#else
    return -2;
#endif
  }
  errno = EINVAL;
  return -1;
}

static int write_all(int fd, const char *buf, size_t len) {
  while (len > 0) {
    ssize_t w = write(fd, buf, len);
    if (w < 0) {
      if (errno == EINTR) continue;
      return -1;
    }
    buf += w;
    len -= (size_t)w;
  }
  return 0;
}

static int mode_features(void) {
  printf("fdatasync=yes full_fsync=%s barrier_fsync=%s\n", has_full_fsync() ? "yes" : "no",
         has_barrier_fsync() ? "yes" : "no");
  return 0;
}

static int mode_dirfd(const char *dir) {
  int wfd = open(dir, O_WRONLY);
  int werr = errno;
  if (wfd >= 0) close(wfd);
  int rfd = open(dir, O_RDONLY);
  if (rfd < 0) {
    printf("wronly=%s rdonly_open=%s\n", wfd >= 0 ? "opened" : errname(werr), errname(errno));
    return 1;
  }
  int s1 = fsync(rfd);
  const char *st1 = status_of(s1);
  printf("wronly=%s rdonly_fsync=%s", wfd >= 0 ? "opened" : errname(werr), st1);
  int s2 = fdatasync(rfd);
  printf(" rdonly_fdatasync=%s\n", status_of(s2));
  close(rfd);
  return 0;
}

static int mode_rdonly(const char *file) {
  int fd = open(file, O_RDONLY);
  if (fd < 0) {
    printf("open=%s\n", errname(errno));
    return 1;
  }
  int s1 = fsync(fd);
  printf("rdonly_fsync=%s", status_of(s1));
  int s2 = fdatasync(fd);
  printf(" rdonly_fdatasync=%s\n", status_of(s2));
  close(fd);
  return 0;
}

static int mode_pipe(void) {
  int p[2];
  if (pipe(p) != 0) {
    printf("pipe=%s\n", errname(errno));
    return 1;
  }
  int s1 = fsync(p[1]);
  printf("fsync=%s", status_of(s1));
  int s2 = fdatasync(p[1]);
  printf(" fdatasync=%s\n", status_of(s2));
  close(p[0]);
  close(p[1]);
  return 0;
}

static int mode_samefile(const char *dir) {
  char a[4096], b[4096];
  struct stat st;
  snprintf(a, sizeof a, "%s/a", dir);
  snprintf(b, sizeof b, "%s/b", dir);
  int fd = open(a, O_WRONLY | O_CREAT | O_EXCL, 0644);
  if (fd < 0 || write_all(fd, "A\n", 2) != 0 || close(fd) != 0) {
    printf("create=%s\n", errname(errno));
    return 1;
  }
  if (link(a, b) != 0) {
    printf("link=%s\n", errname(errno));
    return 1;
  }
  int rc = rename(a, b);
  const char *st_rc = status_of(rc);
  int a_ok = stat(a, &st) == 0;
  int b_ok = stat(b, &st) == 0;
  printf("rename=%s a=%s b=%s nlink=%ld\n", st_rc, a_ok ? "yes" : "no", b_ok ? "yes" : "no",
         b_ok ? (long)st.st_nlink : -1L);
  return 0;
}

/* durable_replace: the four-step protocol; prints one status line. */
static int mode_replace(const char *dir, const char *name, const char *text, const char *method) {
  char tmp[4096], dest[4096];
  struct stat tst, dst;
  snprintf(tmp, sizeof tmp, "%s/.rf.XXXXXX", dir);
  snprintf(dest, sizeof dest, "%s/%s", dir, name);
  int fd = mkstemp(tmp);
  if (fd < 0) {
    printf("create=%s\n", errname(errno));
    return 1;
  }
  if (write_all(fd, text, strlen(text)) != 0 || write_all(fd, "\n", 1) != 0) {
    printf("write=%s\n", errname(errno));
    unlink(tmp);
    return 1;
  }
  if (fchmod(fd, 0644) != 0 || fstat(fd, &tst) != 0) {
    printf("chmod=%s\n", errname(errno));
    unlink(tmp);
    return 1;
  }
  /* Step 1: the data and the inode reach storage before any name points at them. */
  int f = flush_fd(fd, method);
  if (f == -2) {
    printf("flush=%s:unsupported\n", method);
    close(fd);
    unlink(tmp);
    return 2;
  }
  if (f != 0) {
    printf("flush=%s:%s\n", method, errname(errno));
    close(fd);
    unlink(tmp);
    return 1;
  }
  /* close() can report a deferred write error (NFS); a failed close means "do not rename". */
  if (close(fd) != 0) {
    printf("flush=%s:ok close=%s\n", method, errname(errno));
    unlink(tmp);
    return 1;
  }
  /* Step 2: one atomic directory update; readers see the old file or the new one. */
  if (rename(tmp, dest) != 0) {
    printf("flush=%s:ok rename=%s\n", method, errname(errno));
    unlink(tmp);
    return 1;
  }
  int kept = stat(dest, &dst) == 0 && dst.st_ino == tst.st_ino && dst.st_dev == tst.st_dev;
  /* Step 3: make the directory entry itself durable, with the same method, so that on
   * macOS a "full" replace also drains the drive cache after the rename. */
  int dfd = open(dir, O_RDONLY);
  int ds = dfd < 0 ? -1 : flush_fd(dfd, method);
  int derr = errno;
  if (dfd >= 0) close(dfd);
  printf("flush=%s:ok rename=ok ino_kept=%s dirsync=%s\n", method, kept ? "yes" : "no",
         ds == 0 ? (strcmp(method, "none") == 0 ? "skipped" : "ok") : errname(derr));
  return ds == 0 ? 0 : 1;
}

static long now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long)ts.tv_sec * 1000000L + ts.tv_nsec / 1000L;
}

static int cmp_long(const void *x, const void *y) {
  long a = *(const long *)x, b = *(const long *)y;
  return (a > b) - (a < b);
}

static void report(const char *label, long *v, int n) {
  qsort(v, (size_t)n, sizeof *v, cmp_long);
  printf("method=%s n=%d p50_us=%ld max_us=%ld\n", label, n, v[n / 2], v[n - 1]);
}

/* bench_overwrite: rewrite one 4 KiB block in place, then flush; a database page update. */
static int bench_overwrite(const char *dir, const char *method, int n, long *v) {
  char path[4096], block[4096];
  memset(block, 'x', sizeof block);
  snprintf(path, sizeof path, "%s/.bench.XXXXXX", dir);
  int fd = mkstemp(path);
  if (fd < 0) return -1;
  unlink(path);
  if (write_all(fd, block, sizeof block) != 0 || fsync(fd) != 0) {
    close(fd);
    return -1;
  }
  for (int i = 0; i < n; i++) {
    block[0] = (char)('a' + i % 26);
    long t0 = now_us();
    if (pwrite(fd, block, sizeof block, 0) != (ssize_t)sizeof block || flush_fd(fd, method) != 0) {
      close(fd);
      return -1;
    }
    v[i] = now_us() - t0;
  }
  close(fd);
  return 0;
}

/* bench_replace: the whole durable-replace protocol for a 4 KiB file. */
static int bench_replace(const char *dir, const char *method, int n, long *v) {
  char tmp[4096], dest[4096], block[4096];
  memset(block, 'y', sizeof block);
  snprintf(dest, sizeof dest, "%s/.bench-dest", dir);
  for (int i = 0; i < n; i++) {
    long t0 = now_us();
    snprintf(tmp, sizeof tmp, "%s/.bench.XXXXXX", dir);
    int fd = mkstemp(tmp);
    if (fd < 0) return -1;
    if (write_all(fd, block, sizeof block) != 0 || flush_fd(fd, method) != 0 || close(fd) != 0 ||
        rename(tmp, dest) != 0) {
      unlink(tmp);
      return -1;
    }
    int dfd = open(dir, O_RDONLY);
    if (dfd < 0 || flush_fd(dfd, method) != 0) {
      if (dfd >= 0) close(dfd);
      return -1;
    }
    close(dfd);
    v[i] = now_us() - t0;
  }
  unlink(dest);
  return 0;
}

static int mode_bench(const char *dir, int n) {
  const char *methods[] = {"none", "fdatasync", "fsync", "full", "barrier"};
  const char *replace_methods[] = {"none", "fsync", "full"};
  char label[64];
  long *v = malloc(sizeof *v * (size_t)n);
  if (v == NULL) return 1;
  for (size_t m = 0; m < sizeof methods / sizeof *methods; m++) {
    if (strcmp(methods[m], "full") == 0 && !has_full_fsync()) continue;
    if (strcmp(methods[m], "barrier") == 0 && !has_barrier_fsync()) continue;
    if (bench_overwrite(dir, methods[m], n, v) != 0) {
      printf("method=%s error=%s\n", methods[m], errname(errno));
      free(v);
      return 1;
    }
    snprintf(label, sizeof label, "overwrite+%s", methods[m]);
    report(label, v, n);
  }
  for (size_t m = 0; m < sizeof replace_methods / sizeof *replace_methods; m++) {
    if (strcmp(replace_methods[m], "full") == 0 && !has_full_fsync()) continue;
    if (bench_replace(dir, replace_methods[m], n, v) != 0) {
      printf("method=replace+%s error=%s\n", replace_methods[m], errname(errno));
      free(v);
      return 1;
    }
    snprintf(label, sizeof label, "replace+%s", replace_methods[m]);
    report(label, v, n);
  }
  free(v);
  return 0;
}

/* mode_fsync: like FreeBSD fsync(1) and GNU "sync FILE": O_RDONLY | O_NONBLOCK, then flush. */
static int mode_fsync(int argc, char **argv) {
  const char *method = "fsync";
  int i = 0, rc = 0;
  if (i < argc && strcmp(argv[i], "-d") == 0) {
    method = "fdatasync";
    i++;
  } else if (i < argc && strcmp(argv[i], "-f") == 0) {
    method = "full";
    i++;
  }
  if (i < argc && strcmp(argv[i], "--") == 0) i++;
  if (i >= argc) {
    fprintf(stderr, "usage: fsync [-d|-f] PATH...\n");
    return 2;
  }
  for (; i < argc; i++) {
    int fd = open(argv[i], O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
      fprintf(stderr, "fsync: open %s: %s\n", argv[i], strerror(errno));
      rc = 1;
      continue;
    }
    int f = flush_fd(fd, method);
    if (f == -2) {
      fprintf(stderr, "fsync: -f: F_FULLFSYNC is not available on this system\n");
      close(fd);
      return 2;
    }
    if (f != 0) {
      fprintf(stderr, "fsync: %s %s: %s\n", method, argv[i], strerror(errno));
      rc = 1;
    }
    if (close(fd) != 0) rc = 1;
  }
  return rc;
}

int main(int argc, char **argv) {
  const char *base = strrchr(argv[0], '/');
  base = base ? base + 1 : argv[0];
  if (strcmp(base, "fsync") == 0) return mode_fsync(argc - 1, argv + 1);
  if (argc < 2) {
    fprintf(stderr, "usage: fsprobe MODE [ARG...]\n");
    return 2;
  }
  const char *m = argv[1];
  if (strcmp(m, "features") == 0) return mode_features();
  if (strcmp(m, "dirfd") == 0 && argc == 3) return mode_dirfd(argv[2]);
  if (strcmp(m, "rdonly") == 0 && argc == 3) return mode_rdonly(argv[2]);
  if (strcmp(m, "pipe") == 0) return mode_pipe();
  if (strcmp(m, "samefile") == 0 && argc == 3) return mode_samefile(argv[2]);
  if (strcmp(m, "replace") == 0 && argc == 6) return mode_replace(argv[2], argv[3], argv[4], argv[5]);
  if (strcmp(m, "bench") == 0 && argc == 4) {
    int n = atoi(argv[3]);
    return n > 0 ? mode_bench(argv[2], n) : 2;
  }
  if (strcmp(m, "fsync") == 0) return mode_fsync(argc - 2, argv + 2);
  fprintf(stderr, "fsprobe: unknown mode or wrong arguments: %s\n", m);
  return 2;
}
