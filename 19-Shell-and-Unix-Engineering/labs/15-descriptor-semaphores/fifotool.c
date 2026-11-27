/* fifotool: three probes the shell cannot do for itself.
 *   count PATH     print the bytes queued in the FIFO PATH, leaving them queued
 *   capacity PATH  print how many 1-byte writes the FIFO takes before write(2) says EAGAIN
 *   isopen FD      print "open" if descriptor FD is open in this process, else "closed"
 * count and capacity open PATH read-write and non-blocking, so they never wait for a peer.
 * count drains the FIFO and writes the same bytes back, because FIONREAD reports 0 on a macOS
 * FIFO; it is only meaningful while no other process reads or writes the FIFO.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

enum { BUFSZ = 1 << 20 };

static int open_fifo(const char *path)
{
  int fd = open(path, O_RDWR | O_NONBLOCK);
  if (fd < 0) {
    perror("fifotool: open");
    exit(2);
  }
  return fd;
}

static int is_again(void)
{
  return errno == EAGAIN || errno == EWOULDBLOCK;
}

/* Drain fd until EAGAIN into buf; return the byte count. */
static long drain(int fd, char *buf)
{
  long n = 0;
  for (;;) {
    ssize_t r = read(fd, buf + n, BUFSZ - (size_t)n);
    if (r > 0) {
      n += r;
    } else if (r < 0 && is_again()) {
      return n;
    } else {
      perror("fifotool: read");
      exit(2);
    }
  }
}

int main(int argc, char **argv)
{
  if (argc != 3) {
    fprintf(stderr, "usage: fifotool count|capacity PATH | isopen FD\n");
    return 2;
  }
  if (argv[1][0] == 'i') {
    int fd = atoi(argv[2]);
    puts(fcntl(fd, F_GETFD) < 0 ? "closed" : "open");
    return 0;
  }
  int fd = open_fifo(argv[2]);
  char *buf = malloc(BUFSZ);
  if (buf == NULL) {
    return 2;
  }
  if (argv[1][0] == 'c' && argv[1][1] == 'o') {
    long n = drain(fd, buf);
    long done = 0;
    while (done < n) {
      ssize_t w = write(fd, buf + done, (size_t)(n - done));
      if (w < 0) {
        perror("fifotool: write back");
        return 2;
      }
      done += w;
    }
    printf("%ld\n", n);
    return 0;
  }
  if (argv[1][0] == 'c') {
    long n = 0;
    char b = '.';
    for (;;) {
      ssize_t w = write(fd, &b, 1);
      if (w == 1) {
        n++;
      } else if (w < 0 && is_again()) {
        break;
      } else {
        perror("fifotool: write");
        return 2;
      }
    }
    printf("%ld\n", n);
    return 0;
  }
  fprintf(stderr, "fifotool: unknown mode %s\n", argv[1]);
  return 2;
}
