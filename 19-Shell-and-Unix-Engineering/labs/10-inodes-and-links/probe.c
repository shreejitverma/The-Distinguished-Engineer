#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>

static int do_unlinked(const char *dir) {
  char template_buf[1024];
  int written = snprintf(template_buf, sizeof(template_buf), "%s/probe_unlink_XXXXXX", dir);
  if (written < 0 || (size_t)written >= sizeof(template_buf)) {
    fprintf(stderr, "path too long\n");
    return 1;
  }

  int fd = mkstemp(template_buf);
  if (fd < 0) {
    perror("mkstemp");
    return 1;
  }

  const char payload[] = "active inode payload\n";
  ssize_t w = write(fd, payload, sizeof(payload) - 1);
  if (w != (ssize_t)(sizeof(payload) - 1)) {
    perror("write");
    close(fd);
    unlink(template_buf);
    return 1;
  }

  struct stat st_before;
  if (fstat(fd, &st_before) < 0) {
    perror("fstat before");
    close(fd);
    unlink(template_buf);
    return 1;
  }

  if (unlink(template_buf) < 0) {
    perror("unlink");
    close(fd);
    return 1;
  }

  struct stat st_after;
  if (fstat(fd, &st_after) < 0) {
    perror("fstat after");
    close(fd);
    return 1;
  }

  if (lseek(fd, 0, SEEK_SET) < 0) {
    perror("lseek");
    close(fd);
    return 1;
  }

  char read_buf[64];
  memset(read_buf, 0, sizeof(read_buf));
  ssize_t r = read(fd, read_buf, sizeof(read_buf) - 1);
  if (r < 0) {
    perror("read");
    close(fd);
    return 1;
  }

  close(fd);

  printf("before_nlink=%lu\n", (unsigned long)st_before.st_nlink);
  printf("after_nlink=%lu\n", (unsigned long)st_after.st_nlink);
  printf("bytes_read=%zd\n", r);
  printf("payload=%s", read_buf);
  return 0;
}

static int do_link_dir(const char *dirpath, const char *targetpath) {
  if (link(dirpath, targetpath) == 0) {
    printf("success\n");
    unlink(targetpath);
    return 0;
  }

  int err = errno;
  const char *name = "UNKNOWN";
  if (err == EPERM) name = "EPERM";
#ifdef EISDIR
  else if (err == EISDIR) name = "EISDIR";
#endif
#ifdef EOPNOTSUPP
  else if (err == EOPNOTSUPP) name = "EOPNOTSUPP";
#endif
  else if (err == EACCES) name = "EACCES";
  else if (err == EXDEV) name = "EXDEV";

  printf("errno=%s\n", name);
  return 0;
}

static int do_symloop(const char *dir) {
  char p1[1024];
  char p2[1024];
  snprintf(p1, sizeof(p1), "%s/loop_a", dir);
  snprintf(p2, sizeof(p2), "%s/loop_b", dir);

  unlink(p1);
  unlink(p2);

  if (symlink("loop_b", p1) < 0) {
    perror("symlink p1");
    return 1;
  }
  if (symlink("loop_a", p2) < 0) {
    perror("symlink p2");
    unlink(p1);
    return 1;
  }

  int fd = open(p1, O_RDONLY);
  int err = errno;
  if (fd >= 0) {
    printf("open succeeded unexpectedly\n");
    close(fd);
    unlink(p1);
    unlink(p2);
    return 1;
  }

  unlink(p1);
  unlink(p2);

  if (err == ELOOP) {
    printf("errno=ELOOP\n");
    return 0;
  }

  printf("errno=%d\n", err);
  return 0;
}

static int do_readlink(const char *path) {
  char buf[1024];
  /* Fill with a sentinel so a NUL written by readlink(2) would be visible. */
  memset(buf, 'X', sizeof(buf));
  ssize_t len = readlink(path, buf, sizeof(buf) - 1);
  if (len < 0) {
    perror("readlink");
    return 1;
  }
  int nul_written = (buf[len] == '\0');
  buf[len] = '\0';
  printf("target=%s\nlen=%zd\nnul_written=%s\n", buf, len, nul_written ? "yes" : "no");
  return 0;
}

static int do_stat_ids(const char *path) {
  struct stat lst;
  struct stat st;

  if (lstat(path, &lst) < 0) {
    perror("lstat");
    return 1;
  }

  int st_ok = (stat(path, &st) == 0);

  printf("lstat_dev=%llu\n", (unsigned long long)lst.st_dev);
  printf("lstat_ino=%llu\n", (unsigned long long)lst.st_ino);
  printf("lstat_nlink=%lu\n", (unsigned long)lst.st_nlink);
  printf("lstat_is_lnk=%d\n", S_ISLNK(lst.st_mode) ? 1 : 0);

  if (st_ok) {
    printf("stat_dev=%llu\n", (unsigned long long)st.st_dev);
    printf("stat_ino=%llu\n", (unsigned long long)st.st_ino);
    printf("stat_nlink=%lu\n", (unsigned long)st.st_nlink);
    printf("stat_is_lnk=%d\n", S_ISLNK(st.st_mode) ? 1 : 0);
  } else {
    printf("stat_error=%d\n", errno);
  }
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s <unlinked DIR | link_dir DIR TARGET | symloop DIR | readlink PATH | stat_ids PATH>\n", argv[0]);
    return 1;
  }

  const char *cmd = argv[1];
  if (strcmp(cmd, "unlinked") == 0) {
    if (argc < 3) { fprintf(stderr, "unlinked requires DIR\n"); return 1; }
    return do_unlinked(argv[2]);
  } else if (strcmp(cmd, "link_dir") == 0) {
    if (argc < 4) { fprintf(stderr, "link_dir requires DIR TARGET\n"); return 1; }
    return do_link_dir(argv[2], argv[3]);
  } else if (strcmp(cmd, "symloop") == 0) {
    if (argc < 3) { fprintf(stderr, "symloop requires DIR\n"); return 1; }
    return do_symloop(argv[2]);
  } else if (strcmp(cmd, "readlink") == 0) {
    if (argc < 3) { fprintf(stderr, "readlink requires PATH\n"); return 1; }
    return do_readlink(argv[2]);
  } else if (strcmp(cmd, "stat_ids") == 0) {
    if (argc < 3) { fprintf(stderr, "stat_ids requires PATH\n"); return 1; }
    return do_stat_ids(argv[2]);
  }

  fprintf(stderr, "unknown command: %s\n", cmd);
  return 1;
}
