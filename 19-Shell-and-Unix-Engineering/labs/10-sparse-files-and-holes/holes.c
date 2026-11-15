/*
 * holes.c - probe sparse-file mechanics: holes, extents, allocation, and hole punching.
 *
 * Usage:
 *   holes fill PATH OFFSET LENGTH CHAR   write LENGTH copies of CHAR at OFFSET (no truncation)
 *   holes stat PATH                      print size=<st_size> alloc=<st_blocks * 512>
 *   holes map PATH                       walk SEEK_DATA/SEEK_HOLE and print hole/data extents
 *   holes edges PATH                     show the virtual hole at EOF and ENXIO at EOF
 *   holes punch PATH OFFSET LENGTH       deallocate a range, keeping the file size
 *   holes minhole PATH                   print pathconf(_PC_MIN_HOLE_SIZE) where it exists
 *
 * Exit status: 0 success, 1 system call failure, 2 usage error, 3 unsupported here.
 */
#if defined(__linux__)
#define _GNU_SOURCE 1 /* SEEK_DATA, SEEK_HOLE, fallocate() and FALLOC_FL_* on glibc and musl */
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#if defined(__linux__)
#include <linux/falloc.h>
#endif

static int usage(void) {
  fprintf(stderr, "usage: holes fill|stat|map|edges|punch|minhole PATH [ARGS]\n");
  return 2;
}

static int parse_off(const char *s, off_t *out) {
  char *end = NULL;
  long long v;
  errno = 0;
  v = strtoll(s, &end, 10);
  if (errno != 0 || end == s || *end != '\0' || v < 0) {
    fprintf(stderr, "holes: bad number: %s\n", s);
    return -1;
  }
  *out = (off_t)v;
  return 0;
}

static int fail(const char *what, const char *path) {
  fprintf(stderr, "holes: %s %s: %s\n", what, path, strerror(errno));
  return 1;
}

static int do_fill(const char *path, off_t off, off_t len, int ch) {
  char buf[65536];
  int fd = open(path, O_WRONLY | O_CREAT, 0644);
  if (fd < 0)
    return fail("open", path);
  memset(buf, ch, sizeof buf);
  while (len > 0) {
    size_t n = len < (off_t)sizeof buf ? (size_t)len : sizeof buf;
    ssize_t w = pwrite(fd, buf, n, off);
    if (w < 0) {
      int saved = errno;
      close(fd);
      errno = saved;
      return fail("pwrite", path);
    }
    off += w;
    len -= w;
  }
  if (close(fd) != 0)
    return fail("close", path);
  return 0;
}

static int do_stat(const char *path) {
  struct stat st;
  if (stat(path, &st) != 0)
    return fail("stat", path);
  /* Linux and macOS both report st_blocks in 512-byte units; POSIX leaves the unit unspecified. */
  printf("size=%lld alloc=%lld\n", (long long)st.st_size, (long long)st.st_blocks * 512LL);
  return 0;
}

static int do_map(const char *path) {
  struct stat st;
  off_t pos = 0;
  int fd = open(path, O_RDONLY);
  if (fd < 0)
    return fail("open", path);
  if (fstat(fd, &st) != 0)
    return fail("fstat", path);
  while (pos < st.st_size) {
    off_t data = lseek(fd, pos, SEEK_DATA);
    off_t hole;
    if (data < 0) {
      if (errno != ENXIO)
        return fail("lseek SEEK_DATA", path);
      /* ENXIO: no data at or after pos, so the rest of the file is one hole. */
      printf("hole %lld %lld\n", (long long)pos, (long long)st.st_size);
      break;
    }
    if (data > pos)
      printf("hole %lld %lld\n", (long long)pos, (long long)data);
    hole = lseek(fd, data, SEEK_HOLE);
    if (hole < 0)
      return fail("lseek SEEK_HOLE", path);
    printf("data %lld %lld\n", (long long)data, (long long)hole);
    pos = hole;
  }
  printf("end %lld\n", (long long)st.st_size);
  close(fd);
  return 0;
}

static const char *seek_result(int fd, off_t off, int whence, char *buf, size_t bufsz) {
  off_t r = lseek(fd, off, whence);
  if (r >= 0)
    snprintf(buf, bufsz, "%lld", (long long)r);
  else if (errno == ENXIO)
    snprintf(buf, bufsz, "ENXIO");
  else
    snprintf(buf, bufsz, "errno%d", errno);
  return buf;
}

static int do_edges(const char *path) {
  struct stat st;
  char a[32], b[32], c[32];
  int fd = open(path, O_RDONLY);
  if (fd < 0)
    return fail("open", path);
  if (fstat(fd, &st) != 0)
    return fail("fstat", path);
  printf("hole_from_0=%s hole_at_eof=%s data_at_eof=%s\n",
         seek_result(fd, 0, SEEK_HOLE, a, sizeof a),
         seek_result(fd, st.st_size, SEEK_HOLE, b, sizeof b),
         seek_result(fd, st.st_size, SEEK_DATA, c, sizeof c));
  close(fd);
  return 0;
}

static int do_punch(const char *path, off_t off, off_t len) {
  int fd = open(path, O_WRONLY);
  int rc;
  if (fd < 0)
    return fail("open", path);
#if defined(__linux__) && defined(FALLOC_FL_PUNCH_HOLE)
  /* PUNCH_HOLE must be ORed with KEEP_SIZE (fallocate(2)). */
  rc = fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, off, len);
#elif defined(__APPLE__) && defined(F_PUNCHHOLE)
  {
    fpunchhole_t ph;
    memset(&ph, 0, sizeof ph);
    ph.fp_offset = off;
    ph.fp_length = len;
    rc = fcntl(fd, F_PUNCHHOLE, &ph);
  }
#else
  (void)off;
  (void)len;
  close(fd);
  fprintf(stderr, "holes: punch: unsupported on this platform\n");
  return 3;
#endif
  if (rc != 0) {
    int saved = errno;
    close(fd);
    errno = saved;
    if (errno == EINVAL) {
      printf("punch=EINVAL\n");
      return 1;
    }
    return fail("punch", path);
  }
  close(fd);
  printf("punch=ok\n");
  return 0;
}

static int do_minhole(const char *path) {
#if defined(_PC_MIN_HOLE_SIZE)
  long v;
  errno = 0;
  v = pathconf(path, _PC_MIN_HOLE_SIZE);
  if (v < 0 && errno != 0)
    return fail("pathconf", path);
  printf("minhole=%ld\n", v);
#else
  (void)path;
  printf("minhole=undefined\n");
#endif
  return 0;
}

int main(int argc, char **argv) {
  off_t a, b;
  if (argc < 3)
    return usage();
  if (strcmp(argv[1], "fill") == 0 && argc == 6) {
    if (parse_off(argv[3], &a) || parse_off(argv[4], &b) || strlen(argv[5]) != 1)
      return 2;
    return do_fill(argv[2], a, b, (unsigned char)argv[5][0]);
  }
  if (strcmp(argv[1], "stat") == 0 && argc == 3)
    return do_stat(argv[2]);
  if (strcmp(argv[1], "map") == 0 && argc == 3)
    return do_map(argv[2]);
  if (strcmp(argv[1], "edges") == 0 && argc == 3)
    return do_edges(argv[2]);
  if (strcmp(argv[1], "punch") == 0 && argc == 5) {
    if (parse_off(argv[3], &a) || parse_off(argv[4], &b))
      return 2;
    return do_punch(argv[2], a, b);
  }
  if (strcmp(argv[1], "minhole") == 0 && argc == 3)
    return do_minhole(argv[2]);
  return usage();
}
