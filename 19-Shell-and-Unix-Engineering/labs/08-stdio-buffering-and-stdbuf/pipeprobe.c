/*
 * pipeprobe: decide whether a pipeline stage forwards a line before its input ends.
 *
 * Used as the two ends of one pipeline, with a FIFO CTL as a side channel:
 *   pipeprobe feed CTL | STAGE | pipeprobe watch CTL MS
 *
 * feed  writes "hello\n" with write(2), then keeps its end of the pipe open
 *       until watch has finished its early look (it blocks opening CTL for
 *       reading), so STAGE sees one line but no end of file.
 * watch polls its stdin for MS milliseconds and counts the bytes that arrived
 *       ("early"), then opens CTL for writing, which releases feed; feed exits,
 *       STAGE sees end of file, and watch reads to end of file ("total").
 *
 * Output: "early=N total=M". A stage that buffers prints early=0 total=6;
 * a stage that forwards each line prints early=6 total=6.
 * Both modes arm alarm(30), so a broken stage can never hang the pipeline.
 */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int feed(const char *ctl)
{
  static const char line[] = "hello\n";
  char c;
  int fd;
  ssize_t n;

  alarm(30);
  n = write(STDOUT_FILENO, line, sizeof line - 1);
  if (n != (ssize_t)(sizeof line - 1)) {
    return 1;
  }
  /* Blocks until watch opens CTL for writing: that is the release signal. */
  fd = open(ctl, O_RDONLY);
  if (fd < 0) {
    return 1;
  }
  n = read(fd, &c, 1);
  close(fd);
  return n < 0 ? 1 : 0;
}

static int watch(const char *ctl, int ms)
{
  char buf[65536];
  long early = 0;
  long total;
  struct pollfd p;
  ssize_t n;
  int fd;

  alarm(30);
  p.fd = STDIN_FILENO;
  p.events = POLLIN;
  p.revents = 0;
  if (poll(&p, 1, ms) > 0) {
    n = read(STDIN_FILENO, buf, sizeof buf);
    if (n < 0) {
      return 1;
    }
    early = (long)n;
  }
  fd = open(ctl, O_WRONLY);
  if (fd < 0) {
    return 1;
  }
  if (write(fd, "x", 1) != 1) {
    close(fd);
    return 1;
  }
  close(fd);
  total = early;
  for (;;) {
    n = read(STDIN_FILENO, buf, sizeof buf);
    if (n < 0 && errno == EINTR) {
      continue;
    }
    if (n <= 0) {
      break;
    }
    total += (long)n;
  }
  printf("early=%ld total=%ld\n", early, total);
  return 0;
}

int main(int argc, char **argv)
{
  if (argc == 3 && strcmp(argv[1], "feed") == 0) {
    return feed(argv[2]);
  }
  if (argc == 4 && strcmp(argv[1], "watch") == 0) {
    return watch(argv[2], atoi(argv[3]));
  }
  fprintf(stderr, "usage: pipeprobe feed CTL | STAGE | pipeprobe watch CTL MS\n");
  return 2;
}
