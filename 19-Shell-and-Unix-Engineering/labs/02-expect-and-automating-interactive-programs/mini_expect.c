/*
 * mini_expect: the expect loop in one file - start a program on a new pseudo-terminal, then
 * alternate "wait until these bytes appear in its output" with "write these bytes to it".
 *
 * Usage:
 *   mini_expect [-t SECS] [-p] [-c] [-v] STEP... -- CMD [ARG...]
 *
 *   STEP is one of, in dialog order:
 *     -x TEXT   wait until TEXT appears in the output not yet consumed (exact bytes, no pattern
 *               language); the match and everything before it are then consumed
 *     -s TEXT   write TEXT to the program
 *   TEXT understands \r, \n, \\ and \ooo (octal) escapes.
 *   -s steps that come before the first -x are written before CMD is executed (type-ahead that
 *   is already waiting when the program starts), so their order against CMD's output is fixed.
 *
 *   -t SECS   limit for each wait, in seconds (default 10, the default of expect's timeout variable)
 *   -p        connect CMD through two pipes instead of a pseudo-terminal (no terminal at all)
 *   -c        close the connection right after the last step, as an expect script does when it
 *             ends; without -c the last step is followed by a wait for end of file
 *   -v        print one line per match: matched=TEXT end=OFFSET reads=N, where OFFSET is the
 *             transcript offset just past the match and N counts the reads the wait needed
 *
 * Output: the -v lines, then
 *   transcript=BYTES   everything CMD wrote, with \r, \n, \\ and \xHH escapes
 *   result=ok status=N         the dialog finished and CMD exited with N
 *   result=closed status=N     the same, with -c
 *   ... status=N signal=S      appended to either line when CMD did not exit but was killed by
 *                              signal number S; N is then 128 + S
 *   result=timeout waiting=T   a wait reached its limit; CMD's process group was killed
 *   result=eof waiting=T       CMD closed the connection before T appeared
 * Exit status: CMD's status, 124 on timeout, 125 on end of file before a match, 2 on a setup error.
 *
 * Every wait is bounded, and CMD's process group is killed before mini_expect returns.
 */
#define _GNU_SOURCE /* glibc declares posix_openpt, grantpt, unlockpt, and ptsname only on request */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define CAPACITY 65536 /* expect instead forgets the oldest third once match_max (2000) fills */
#define MAX_STEPS 64

struct step {
  int is_send;
  char text[1024];
  size_t len;
};

static char transcript[CAPACITY];
static size_t total;    /* bytes read from the program so far */
static size_t consumed; /* offset just past the last match */

/* Decode \r, \n, \\ and \ooo in IN; return the length, or (size_t)-1 if it does not fit in CAP. */
static size_t unescape(const char *in, char *out, size_t cap) {
  size_t len = 0;
  while (*in) {
    if (len >= cap) {
      return (size_t)-1;
    }
    if (*in != '\\') {
      out[len++] = *in++;
      continue;
    }
    in++;
    if (*in == 'n') {
      out[len++] = '\n';
      in++;
    } else if (*in == 'r') {
      out[len++] = '\r';
      in++;
    } else if (*in == '\\') {
      out[len++] = '\\';
      in++;
    } else if (*in >= '0' && *in <= '7') {
      int v = 0;
      for (int i = 0; i < 3 && *in >= '0' && *in <= '7'; i++, in++) {
        v = v * 8 + (*in - '0');
      }
      out[len++] = (char)v;
    } else {
      out[len++] = '\\';
    }
  }
  return len;
}

/* Print BUF[0..N) with \r, \n, \\ and \xHH escapes. */
static void show(const char *buf, size_t n) {
  for (size_t i = 0; i < n; i++) {
    unsigned char c = (unsigned char)buf[i];
    if (c == '\r') {
      fputs("\\r", stdout);
    } else if (c == '\n') {
      fputs("\\n", stdout);
    } else if (c == '\\') {
      fputs("\\\\", stdout);
    } else if (c < 0x20 || c > 0x7e) {
      printf("\\x%02x", c);
    } else {
      putchar(c);
    }
  }
}

static long now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int write_all(int fd, const char *buf, size_t n) {
  while (n > 0) {
    ssize_t w = write(fd, buf, n);
    if (w < 0) {
      if (errno == EINTR) {
        continue;
      }
      return -1;
    }
    buf += w;
    n -= (size_t)w;
  }
  return 0;
}

/* The offset of TEXT in the unconsumed output, or -1. */
static long find(const char *text, size_t len) {
  if (len == 0) {
    return (long)consumed;
  }
  for (size_t i = consumed; i + len <= total; i++) {
    if (memcmp(transcript + i, text, len) == 0) {
      return (long)i;
    }
  }
  return -1;
}

enum { GOT_MATCH, GOT_EOF, GOT_TIMEOUT, GOT_OVERFLOW };

/*
 * The heart of expect: read until TEXT (or, when TEXT is NULL, end of file) or the deadline.
 * The search runs over everything unconsumed, so a prompt that arrives in two reads still matches.
 * *READS counts the reads that returned data.
 */
static int wait_for(int fd, const char *text, size_t len, int secs, int *reads) {
  long deadline = now_ms() + (long)secs * 1000;
  *reads = 0;
  for (;;) {
    if (text != NULL) {
      long at = find(text, len);
      if (at >= 0) {
        consumed = (size_t)at + len;
        return GOT_MATCH;
      }
    }
    long left = deadline - now_ms();
    if (left <= 0) {
      return GOT_TIMEOUT;
    }
    struct pollfd pf = {.fd = fd, .events = POLLIN};
    int pr = poll(&pf, 1, (int)left);
    if (pr < 0 && errno == EINTR) {
      continue;
    }
    if (pr <= 0) {
      return GOT_TIMEOUT;
    }
    if (total >= CAPACITY) {
      return GOT_OVERFLOW;
    }
    ssize_t n = read(fd, transcript + total, CAPACITY - total);
    if (n < 0 && errno == EINTR) {
      continue;
    }
    if (n <= 0) {
      /* 0 is end of file (pipes, macOS ptys); EIO is how a Linux pty reports that every slave closed */
      return GOT_EOF;
    }
    total += (size_t)n;
    (*reads)++;
  }
}

/* Open a pty master and name its slave: the POSIX sequence. Returns the master or -1. */
static int open_master(char *name, size_t len) {
  int master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0) {
    return -1;
  }
  const char *slave = NULL;
  if (grantpt(master) == 0 && unlockpt(master) == 0) {
    slave = ptsname(master);
  }
  if (slave == NULL) {
    close(master);
    return -1;
  }
  snprintf(name, len, "%s", slave);
  return master;
}

/* Child side of a pty spawn: new session, the slave as controlling terminal and as fds 0, 1, 2. */
static void child_on_pty(const char *name) {
  struct winsize ws = {24, 80, 0, 0};
  if (setsid() < 0) {
    _exit(126);
  }
  int slave = open(name, O_RDWR | O_NOCTTY);
  if (slave < 0 || ioctl(slave, TIOCSCTTY, 0) < 0) {
    _exit(126);
  }
  /* a fresh pair is 0 rows by 0 columns; macOS accepts the size only through the open slave */
  if (ioctl(slave, TIOCSWINSZ, &ws) < 0) {
    _exit(126);
  }
  if (dup2(slave, 0) < 0 || dup2(slave, 1) < 0 || dup2(slave, 2) < 0) {
    _exit(126);
  }
  if (slave > 2) {
    close(slave);
  }
}

/* Child side of a pipe spawn: new session (so the group can be killed), pipes as fds 0 and 1. */
static void child_on_pipes(int in, int out) {
  if (setsid() < 0) {
    _exit(126);
  }
  if (dup2(in, 0) < 0 || dup2(out, 1) < 0 || dup2(out, 2) < 0) {
    _exit(126);
  }
  if (in > 2) {
    close(in);
  }
  if (out > 2) {
    close(out);
  }
}

/*
 * Wait at most SECS for PID to exit, then kill its process group and reap it. Returns its exit
 * status, or 128 + the signal number when a signal killed it; *TERMSIG is that signal number, or 0
 * when it exited, so "exited with 129" and "killed by signal 1" stay apart.
 */
static int reap(pid_t pid, int secs, int *termsig) {
  int status = 0;
  *termsig = 0;
  long deadline = now_ms() + (long)secs * 1000;
  for (;;) {
    pid_t r = waitpid(pid, &status, WNOHANG);
    if (r == pid) {
      break;
    }
    if (r < 0 && errno != EINTR) {
      status = 0;
      break;
    }
    if (now_ms() >= deadline) {
      kill(-pid, SIGKILL);
      waitpid(pid, &status, 0);
      break;
    }
    poll(NULL, 0, 20);
  }
  kill(-pid, SIGKILL); /* whatever else is left in the program's group */
  if (WIFSIGNALED(status)) {
    *termsig = WTERMSIG(status);
    return 128 + *termsig;
  }
  return WEXITSTATUS(status);
}

int main(int argc, char **argv) {
  static struct step steps[MAX_STEPS];
  int nsteps = 0, secs = 10, use_pipes = 0, close_early = 0, verbose = 0, i = 1;
  for (; i < argc && strcmp(argv[i], "--") != 0; i++) {
    if (strcmp(argv[i], "-p") == 0) {
      use_pipes = 1;
    } else if (strcmp(argv[i], "-c") == 0) {
      close_early = 1;
    } else if (strcmp(argv[i], "-v") == 0) {
      verbose = 1;
    } else if (strcmp(argv[i], "-t") == 0 && i + 1 < argc) {
      secs = atoi(argv[++i]);
    } else if ((strcmp(argv[i], "-x") == 0 || strcmp(argv[i], "-s") == 0) && i + 1 < argc && nsteps < MAX_STEPS) {
      struct step *st = &steps[nsteps++];
      st->is_send = argv[i][1] == 's';
      st->len = unescape(argv[++i], st->text, sizeof st->text);
      if (st->len == (size_t)-1) {
        fprintf(stderr, "mini_expect: step text too long\n");
        return 2;
      }
    } else {
      fprintf(stderr, "mini_expect: bad option %s\n", argv[i]);
      return 2;
    }
  }
  if (i >= argc - 1 || secs <= 0) {
    fprintf(stderr, "usage: mini_expect [-t SECS] [-p] [-c] [-v] [-x TEXT | -s TEXT]... -- CMD [ARG...]\n");
    return 2;
  }
  char **cmd = &argv[i + 1];

  int rfd, wfd, to_child[2] = {-1, -1}, from_child[2] = {-1, -1};
  char name[128] = "";
  if (use_pipes) {
    if (pipe(to_child) < 0 || pipe(from_child) < 0) {
      fprintf(stderr, "mini_expect: pipe: %s\n", strerror(errno));
      return 2;
    }
    rfd = from_child[0];
    wfd = to_child[1];
  } else {
    rfd = wfd = open_master(name, sizeof name);
    if (rfd < 0) {
      fprintf(stderr, "mini_expect: cannot open a pty: %s\n", strerror(errno));
      return 2;
    }
  }
  /* Two pipes hold CMD back until the type-ahead is written: the child reports that its side is
     set up (a pty slave must be open before bytes written to the master are kept), then blocks
     until the parent closes "go". */
  int ready[2], go[2];
  if (pipe(ready) < 0 || pipe(go) < 0) {
    fprintf(stderr, "mini_expect: pipe: %s\n", strerror(errno));
    return 2;
  }
  signal(SIGPIPE, SIG_IGN); /* a send to a program that has gone must fail, not kill us */
  fflush(stdout);
  pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "mini_expect: fork: %s\n", strerror(errno));
    return 2;
  }
  if (pid == 0) {
    char c = 'r';
    signal(SIGPIPE, SIG_DFL);
    close(ready[0]);
    close(go[1]);
    if (use_pipes) {
      close(rfd);
      close(wfd);
      child_on_pipes(to_child[0], from_child[1]);
    } else {
      close(rfd);
      child_on_pty(name);
    }
    if (write(ready[1], &c, 1) != 1) {
      _exit(126);
    }
    close(ready[1]);
    if (read(go[0], &c, 1) < 0) {
      _exit(126);
    }
    close(go[0]);
    execvp(cmd[0], cmd);
    _exit(127);
  }
  close(ready[1]);
  close(go[0]);
  if (use_pipes) {
    close(to_child[0]);
    close(from_child[1]);
  }
  char ack;
  if (read(ready[0], &ack, 1) != 1) {
    /* the child failed before exec; the first wait reports the end of file */
  }
  close(ready[0]);

  int first = 0;
  while (first < nsteps && steps[first].is_send) {
    if (write_all(wfd, steps[first].text, steps[first].len) < 0) {
      /* reported by the first wait */
    }
    first++;
  }
  if (first > 0 && !use_pipes) {
    /* let the terminal echo the type-ahead before CMD can write anything: a fresh slave has ECHO
       on, so the echo becomes readable (at once on macOS, after a kernel worker ran on Linux) and
       ends this wait; the 2 second cap is only the margin for a loaded machine */
    struct pollfd pf = {.fd = rfd, .events = POLLIN};
    poll(&pf, 1, 2000);
  }
  close(go[1]); /* CMD starts now */

  int outcome = GOT_MATCH, reads = 0;
  const struct step *failed = NULL;
  for (int k = first; k < nsteps && outcome == GOT_MATCH; k++) {
    const struct step *st = &steps[k];
    if (st->is_send) {
      if (write_all(wfd, st->text, st->len) < 0) {
        /* the program has gone; the next wait reports the end of file */
      }
      continue;
    }
    outcome = wait_for(rfd, st->text, st->len, secs, &reads);
    if (outcome != GOT_MATCH) {
      failed = st;
    } else if (verbose) {
      printf("matched=");
      show(st->text, st->len);
      printf(" end=%lu reads=%d\n", (unsigned long)consumed, reads);
    }
  }

  int status = 0, termsig = 0, rc;
  if (outcome == GOT_MATCH && close_early) {
    close(wfd);
    if (rfd != wfd) {
      close(rfd);
    }
    status = reap(pid, secs, &termsig);
  } else if (outcome == GOT_MATCH) {
    if (use_pipes) {
      close(wfd); /* end of file on the program's stdin, as a here-document gives */
    }
    outcome = wait_for(rfd, NULL, 0, secs, &reads);
    if (outcome == GOT_EOF) {
      status = reap(pid, secs, &termsig);
    }
  }
  if (outcome == GOT_TIMEOUT || outcome == GOT_OVERFLOW || (outcome == GOT_EOF && failed != NULL)) {
    kill(-pid, SIGKILL);
    waitpid(pid, NULL, 0);
  }

  printf("transcript=");
  show(transcript, total);
  putchar('\n');
  if (outcome == GOT_TIMEOUT) {
    printf("result=timeout waiting=");
    if (failed != NULL) {
      show(failed->text, failed->len);
    } else {
      fputs("eof", stdout);
    }
    putchar('\n');
    rc = 124;
  } else if (outcome == GOT_OVERFLOW) {
    printf("result=overflow\n");
    rc = 2;
  } else if (failed != NULL) {
    printf("result=eof waiting=");
    show(failed->text, failed->len);
    putchar('\n');
    rc = 125;
  } else {
    printf("result=%s status=%d", close_early ? "closed" : "ok", status);
    if (termsig != 0) {
      printf(" signal=%d", termsig);
    }
    putchar('\n');
    rc = status;
  }
  return rc;
}
