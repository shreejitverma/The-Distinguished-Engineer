/*
 * bufsize: report the buffering libc gives stdout or stderr on a pipe.
 *
 *   bufsize stdout | bufsize stderr
 *
 * The probe makes a pipe, puts its write end on fd 1 (or 2) before the stream
 * is first used, and then writes one byte at a time with fputc, asking the
 * kernel after each byte how many bytes the pipe holds (ioctl FIONREAD).
 * Nothing reaches the pipe until the stream flushes, so the first nonzero
 * count shows when, and how much, the stream wrote:
 *   after "a"          -> mode=unbuffered
 *   after "a\n"        -> mode=line, then fill with "x" to find the size
 *   only when full     -> mode=full size=N (N is the first write)
 * Output goes to fd 3, which the caller must open (for example 3>&1).
 * Sizes above 16384 are not probed: the probe is also the pipe's only reader.
 */
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define LIMIT 16384

static int pending(int rfd)
{
  int n = 0;
  if (ioctl(rfd, FIONREAD, &n) < 0) {
    return -1;
  }
  return n;
}

static int report(const char *text)
{
  size_t len = strlen(text);
  return write(3, text, len) == (ssize_t)len ? 0 : 1;
}

int main(int argc, char **argv)
{
  int p[2];
  int fd;
  FILE *s;
  char drain[LIMIT];
  char msg[64];
  const char *mode;
  int i;
  int n;

  if (argc != 2 || (strcmp(argv[1], "stdout") != 0 && strcmp(argv[1], "stderr") != 0)) {
    fprintf(stderr, "usage: bufsize stdout|stderr 3>&1\n");
    return 2;
  }
  fd = strcmp(argv[1], "stdout") == 0 ? STDOUT_FILENO : STDERR_FILENO;
  s = fd == STDOUT_FILENO ? stdout : stderr;
  alarm(10);
  if (pipe(p) < 0 || dup2(p[1], fd) < 0) {
    return 1;
  }
  close(p[1]);

  fputc('a', s);
  if ((n = pending(p[0])) != 0) {
    return report(n > 0 ? "mode=unbuffered\n" : "error\n");
  }
  fputc('\n', s);
  n = pending(p[0]);
  if (n < 0) {
    return report("error\n");
  }
  mode = "full";
  if (n > 0) {
    mode = "line";
    if (read(p[0], drain, (size_t)n) != n) {
      return report("error\n");
    }
  }
  for (i = 0; i < LIMIT; i++) {
    fputc('x', s);
    n = pending(p[0]);
    if (n != 0) {
      break;
    }
  }
  if (n > 0) {
    snprintf(msg, sizeof msg, "mode=%s size=%d\n", mode, n);
  } else {
    snprintf(msg, sizeof msg, "mode=%s size=over-%d\n", mode, LIMIT);
  }
  if (report(msg) != 0) {
    return 1;
  }
  /* Discard what is still buffered: the pipe has no other reader. */
  _exit(0);
}
