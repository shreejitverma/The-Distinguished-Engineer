/*
 * rprobe: the system-call view of symbolic-link handling.
 *
 *   rprobe open PATH         open(PATH, O_RDONLY); print "ok" or the errno name
 *   rprobe nofollow PATH     open(PATH, O_RDONLY | O_NOFOLLOW); same output
 *   rprobe walk DIR RELPATH  open DIR, then each component of RELPATH with openat(O_NOFOLLOW),
 *                            so no symlink anywhere in RELPATH is followed and ".." cannot
 *                            climb out; print the file's first line or "refused COMPONENT: WHY"
 *
 * walk is the portable userland form of Linux openat2(RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH).
 * Exit status: 0 on success, 1 on a refused or failed open, 2 on bad usage.
 */
#define _POSIX_C_SOURCE 200809L
#if defined(__APPLE__)
#define _DARWIN_C_SOURCE /* macOS hides O_NOFOLLOW under a strict _POSIX_C_SOURCE */
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *errname(int e) {
  switch (e) {
    case ELOOP: return "ELOOP";
    case EMLINK: return "EMLINK"; /* FreeBSD's O_NOFOLLOW error */
    case ENOENT: return "ENOENT";
    case ENOTDIR: return "ENOTDIR";
    case EACCES: return "EACCES";
    default: return "other";
  }
}

static int report_open(const char *path, int flags) {
  int fd = open(path, flags);
  if (fd < 0) {
    printf("%s\n", errname(errno));
    return 1;
  }
  close(fd);
  printf("ok\n");
  return 0;
}

/* Explain a refused openat: the open itself already failed safely; this lstat-like call
 * only labels the reason and decides nothing. */
static int refuse(int dirfd, const char *comp, int err) {
  struct stat st;
  if (fstatat(dirfd, comp, &st, AT_SYMLINK_NOFOLLOW) == 0 && S_ISLNK(st.st_mode))
    printf("refused %s: symbolic link\n", comp);
  else
    printf("refused %s: %s\n", comp, errname(err));
  return 1;
}

static int walk(const char *dir, const char *relpath) {
  char buf[4096];
  char line[256];
  char *comp, *next;
  int fd, nfd, last;
  ssize_t n;

  if (relpath[0] == '/') {
    printf("refused %s: absolute\n", relpath);
    return 1;
  }
  if (strlen(relpath) >= sizeof buf) {
    printf("refused: too long\n");
    return 1;
  }
  strcpy(buf, relpath);
  fd = open(dir, O_RDONLY | O_DIRECTORY);
  if (fd < 0) {
    printf("cannot open %s: %s\n", dir, errname(errno));
    return 1;
  }
  for (comp = buf;; comp = next) {
    next = strchr(comp, '/');
    last = next == NULL;
    if (!last)
      *next++ = '\0';
    if (comp[0] == '\0' || strcmp(comp, ".") == 0) {
      if (last)
        break;
      continue;
    }
    if (strcmp(comp, "..") == 0) {
      printf("refused ..: dot-dot\n");
      close(fd);
      return 1;
    }
    nfd = openat(fd, comp, last ? (O_RDONLY | O_NOFOLLOW) : (O_RDONLY | O_DIRECTORY | O_NOFOLLOW));
    if (nfd < 0) {
      int err = errno;
      int rc = refuse(fd, comp, err);
      close(fd);
      return rc;
    }
    close(fd);
    fd = nfd;
    if (last)
      break;
  }
  n = read(fd, line, sizeof line - 1);
  close(fd);
  if (n < 0) {
    printf("read failed: %s\n", errname(errno));
    return 1;
  }
  line[n] = '\0';
  line[strcspn(line, "\n")] = '\0';
  printf("%s\n", line);
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 3 && strcmp(argv[1], "open") == 0)
    return report_open(argv[2], O_RDONLY);
  if (argc == 3 && strcmp(argv[1], "nofollow") == 0)
    return report_open(argv[2], O_RDONLY | O_NOFOLLOW);
  if (argc == 4 && strcmp(argv[1], "walk") == 0)
    return walk(argv[2], argv[3]);
  fprintf(stderr, "usage: rprobe open|nofollow PATH | rprobe walk DIR RELPATH\n");
  return 2;
}
