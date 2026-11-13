/*
 * fdprobe: what kind of file does a pathname name, and does it survive a consumer
 * that closes its inherited descriptors?
 *
 * Usage: fdprobe stat PATH      print type=<fifo|regular|other> seek=<ESPIPE|size> data=<line>
 *        fdprobe closeall PATH  close every descriptor from 3 up, then print open=<ok|ERRNO>
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *errno_name(int err) {
  switch (err) {
  case ENOENT:
    return "ENOENT";
  case EBADF:
    return "EBADF";
  case ESPIPE:
    return "ESPIPE";
  case ENXIO:
    return "ENXIO";
  default:
    return "OTHER";
  }
}

static int probe_stat(const char *path) {
  struct stat st;
  char data[64];
  size_t used = 0;
  off_t end;
  int fd = open(path, O_RDONLY);

  if (fd < 0 || fstat(fd, &st) < 0) {
    printf("open=%s\n", errno_name(errno));
    return 1;
  }
  printf("type=%s", S_ISFIFO(st.st_mode) ? "fifo" : S_ISREG(st.st_mode) ? "regular" : "other");
  end = lseek(fd, 0, SEEK_END);
  if (end < 0) {
    printf(" seek=%s", errno_name(errno));
  } else {
    printf(" seek=%lld", (long long)end);
    if (lseek(fd, 0, SEEK_SET) < 0) {
      return 1;
    }
  }
  /* Read to end of file, or until the buffer is full: a pipe can return short reads. */
  while (used < sizeof(data) - 1) {
    ssize_t got = read(fd, data + used, sizeof(data) - 1 - used);
    if (got <= 0) {
      break;
    }
    used += (size_t)got;
  }
  data[used] = '\0';
  data[strcspn(data, "\n")] = '\0';
  printf(" data=%s\n", data);
  close(fd);
  return 0;
}

static int probe_closeall(const char *path) {
  long limit = sysconf(_SC_OPEN_MAX);
  int fd;

  if (limit < 0 || limit > 65536) {
    limit = 65536;
  }
  for (fd = 3; fd < limit; fd++) {
    close(fd); /* EBADF for a descriptor that was not open is the common, harmless case */
  }
  fd = open(path, O_RDONLY);
  if (fd < 0) {
    printf("open=%s\n", errno_name(errno));
    return 0;
  }
  printf("open=ok\n");
  close(fd);
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 3 && strcmp(argv[1], "stat") == 0) {
    return probe_stat(argv[2]);
  }
  if (argc == 3 && strcmp(argv[1], "closeall") == 0) {
    return probe_closeall(argv[2]);
  }
  fprintf(stderr, "usage: fdprobe stat|closeall PATH\n");
  return 2;
}
