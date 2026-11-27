/* fifo-probe: open, poll, capacity, and framing probes for FIFOs (named pipes).
 *
 * Usage:
 *   fifo-probe open-flags FIFO            open(2) outcomes with and without a peer
 *   fifo-probe poll-hup FIFO              poll(2) revents, and select(2), around a writer lifetime
 *   fifo-probe capacity FIFO              bytes accepted before EAGAIN
 *   fifo-probe write-lines FIFO CH N LEN  N lines of LEN bytes (LEN-1 x CH, then newline),
 *                                         one write(2) per line, blocking open for writing
 *
 * Every mode arms alarm(10) so a blocked open(2) kills the probe instead of hanging the suite.
 * The FIFO must already exist (create it with mkfifo(1)).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <unistd.h>

static const char *errname(int e)
{
  static char buf[32];
  if (e == ENXIO) {
    return "ENXIO";
  }
  if (e == EAGAIN) {
    return "EAGAIN";
  }
  if (e == EEXIST) {
    return "EEXIST";
  }
  snprintf(buf, sizeof buf, "errno-%d", e);
  return buf;
}

static int die(const char *what)
{
  fprintf(stderr, "fifo-probe: %s: %s\n", what, strerror(errno));
  return 1;
}

static int mode_open_flags(const char *path)
{
  char c;
  ssize_t n;
  int r, w, rw;

  w = open(path, O_WRONLY | O_NONBLOCK);
  if (w >= 0) {
    printf("wronly-nonblock-no-reader=ok\n");
    close(w);
  } else {
    printf("wronly-nonblock-no-reader=%s\n", errname(errno));
  }

  r = open(path, O_RDONLY | O_NONBLOCK);
  if (r < 0) {
    return die("open O_RDONLY|O_NONBLOCK");
  }
  printf("rdonly-nonblock-no-writer=ok\n");
  n = read(r, &c, 1);
  printf("read-no-writer=%ld\n", (long)n);

  w = open(path, O_WRONLY | O_NONBLOCK);
  if (w < 0) {
    return die("open O_WRONLY|O_NONBLOCK with a reader");
  }
  printf("wronly-nonblock-with-reader=ok\n");
  n = read(r, &c, 1);
  if (n < 0) {
    printf("read-empty-with-writer=%s\n", errname(errno));
  } else {
    printf("read-empty-with-writer=%ld\n", (long)n);
  }
  if (write(w, "x", 1) != 1) {
    return die("write");
  }
  n = read(r, &c, 1);
  printf("loopback=%c\n", n == 1 ? c : '?');
  close(w);
  n = read(r, &c, 1);
  printf("read-after-last-writer-closed=%ld\n", (long)n);
  close(r);

  rw = open(path, O_RDWR);
  if (rw < 0) {
    printf("rdwr-blocking-no-peer=%s\n", errname(errno));
  } else {
    printf("rdwr-blocking-no-peer=ok\n");
    close(rw);
  }
  return 0;
}

static const char *pollname(short revents)
{
  static char buf[32];
  buf[0] = '\0';
  if (revents & POLLIN) {
    strcat(buf, "IN");
  }
  if (revents & POLLHUP) {
    strcat(buf, buf[0] ? "|HUP" : "HUP");
  }
  if (revents & POLLERR) {
    strcat(buf, buf[0] ? "|ERR" : "ERR");
  }
  return buf[0] ? buf : "none";
}

static int poll_once(int fd, const char *label)
{
  struct pollfd p;
  p.fd = fd;
  p.events = POLLIN;
  p.revents = 0;
  if (poll(&p, 1, 0) < 0) {
    return die("poll");
  }
  printf("%s=%s\n", label, pollname(p.revents));
  return 0;
}

static int mode_poll_hup(const char *path)
{
  char c;
  int r, w;

  r = open(path, O_RDONLY | O_NONBLOCK);
  if (r < 0) {
    return die("open reader");
  }
  if (poll_once(r, "poll-reader-before-any-writer")) {
    return 1;
  }
  w = open(path, O_WRONLY | O_NONBLOCK);
  if (w < 0) {
    return die("open writer");
  }
  if (poll_once(r, "poll-reader-writer-open-empty")) {
    return 1;
  }
  if (write(w, "x", 1) != 1) {
    return die("write");
  }
  if (poll_once(r, "poll-reader-writer-open-data")) {
    return 1;
  }
  if (read(r, &c, 1) != 1) {
    return die("read");
  }
  close(w);
  if (poll_once(r, "poll-reader-writer-closed")) {
    return 1;
  }
  {
    fd_set rs;
    struct timeval tv;
    FD_ZERO(&rs);
    FD_SET(r, &rs);
    tv.tv_sec = 0;
    tv.tv_usec = 0;
    if (select(r + 1, &rs, NULL, NULL, &tv) < 0) {
      return die("select");
    }
    printf("select-reader-writer-closed=%s\n", FD_ISSET(r, &rs) ? "readable" : "not-readable");
  }
  close(r);
  return 0;
}

static int mode_capacity(const char *path)
{
  long total = 0;
  int r, w;

  r = open(path, O_RDONLY | O_NONBLOCK);
  if (r < 0) {
    return die("open reader");
  }
  w = open(path, O_WRONLY | O_NONBLOCK);
  if (w < 0) {
    return die("open writer");
  }
  for (;;) {
    ssize_t n = write(w, "x", 1);
    if (n == 1) {
      total++;
    } else if (n < 0 && errno == EAGAIN) {
      break;
    } else {
      return die("write");
    }
  }
  printf("bytes-before-eagain=%ld\n", total);
#ifdef F_GETPIPE_SZ
  printf("f-getpipe-sz=%d\n", fcntl(w, F_GETPIPE_SZ));
#else
  printf("f-getpipe-sz=n/a\n");
#endif
  close(w);
  close(r);
  return 0;
}

static int mode_write_lines(const char *path, char ch, long count, long len)
{
  char *line;
  long i;
  int w;

  if (len < 2 || count < 1) {
    fprintf(stderr, "fifo-probe: LEN must be >= 2 and N >= 1\n");
    return 2;
  }
  line = malloc((size_t)len);
  if (line == NULL) {
    return die("malloc");
  }
  memset(line, ch, (size_t)len - 1);
  line[len - 1] = '\n';
  w = open(path, O_WRONLY);
  if (w < 0) {
    return die("open writer");
  }
  for (i = 0; i < count; i++) {
    if (write(w, line, (size_t)len) != (ssize_t)len) {
      return die("write");
    }
  }
  close(w);
  free(line);
  return 0;
}

int main(int argc, char **argv)
{
  alarm(10);
  if (argc == 3 && strcmp(argv[1], "open-flags") == 0) {
    return mode_open_flags(argv[2]);
  }
  if (argc == 3 && strcmp(argv[1], "poll-hup") == 0) {
    return mode_poll_hup(argv[2]);
  }
  if (argc == 3 && strcmp(argv[1], "capacity") == 0) {
    return mode_capacity(argv[2]);
  }
  if (argc == 6 && strcmp(argv[1], "write-lines") == 0 && argv[3][0] != '\0') {
    return mode_write_lines(argv[2], argv[3][0], atol(argv[4]), atol(argv[5]));
  }
  fprintf(stderr, "usage: fifo-probe open-flags|poll-hup|capacity FIFO | write-lines FIFO CH N LEN\n");
  return 2;
}
