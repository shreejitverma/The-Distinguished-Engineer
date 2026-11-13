/*
 * inject_probe: demonstrate the terminal-escape-injection primitive on a real
 * pseudo-terminal.  A terminal that answers a query writes its reply onto the
 * program's input queue, indistinguishable from typing; a line terminator in
 * the reply completes a line the program reads with no human keystroke.
 *
 *   inject_probe term CMD [ARG...]
 *       Run CMD with the slave side of a pty as its controlling terminal,
 *       stdin, stdout and stderr, in canonical mode with echo OFF.  Wait for
 *       the child to emit a title query (ESC [ 21 t), then act as a hostile
 *       terminal and answer with the single injected line  h4ck<CR>.  The CR
 *       is mapped to NL by the line discipline (ICRNL), so the child's read
 *       returns a completed line.  Print the child transcript and exit status.
 *
 *   inject_probe term2 CMD [ARG...]
 *       Like term, but the reply is two injected lines  a<CR>b<CR>, so a single
 *       reply queues two complete input lines (two commands) for the child.
 *
 *   inject_probe show CMD [ARG...]
 *       Run CMD on the same kind of pty and never reply: print what CMD wrote
 *       to its terminal.  This is how the lab sees what a tool such as ls
 *       prints when its standard output is a terminal rather than a pipe.
 *
 * The slave keeps echo off so the transcript holds only the child's own output.
 * The transcript prints ESC as <ESC>, CR as \r, LF as \n, other controls as
 * \xNN.  Every wait is bounded; the child is killed at the deadline.
 */
#ifdef __APPLE__
#define _DARWIN_C_SOURCE 1
#else
#define _GNU_SOURCE 1
#endif
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define TRANSCRIPT_MAX 8192
#define DEADLINE_SECONDS 15

static void print_escaped(const char *buf, size_t len) {
  size_t i;
  for (i = 0; i < len; i++) {
    unsigned char c = (unsigned char)buf[i];
    if (c == 0x1B) {
      fputs("<ESC>", stdout);
    } else if (c == '\r') {
      fputs("\\r", stdout);
    } else if (c == '\n') {
      fputs("\\n", stdout);
    } else if (c < 0x20 || c == 0x7F) {
      printf("\\x%02X", c);
    } else {
      putchar(c);
    }
  }
}

static int open_pair(int *master, int *slave, char *name, size_t name_len) {
  const char *sname;
  *master = posix_openpt(O_RDWR | O_NOCTTY);
  if (*master < 0) {
    return -1;
  }
  if (grantpt(*master) < 0 || unlockpt(*master) < 0) {
    return -1;
  }
  sname = ptsname(*master);
  if (sname == NULL) {
    return -1;
  }
  snprintf(name, name_len, "%s", sname);
  *slave = open(name, O_RDWR | O_NOCTTY);
  return *slave < 0 ? -1 : 0;
}

static double now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static const char *find_bytes(const char *buf, size_t len, const char *needle) {
  size_t nlen = strlen(needle);
  size_t i;
  if (nlen == 0 || len < nlen) {
    return NULL;
  }
  for (i = 0; i + nlen <= len; i++) {
    if (memcmp(buf + i, needle, nlen) == 0) {
      return buf + i;
    }
  }
  return NULL;
}

static int send_reply(int master, const char *reply, size_t len) {
  if (write(master, reply, len) != (ssize_t)len) {
    fprintf(stderr, "inject_probe: short write of a reply\n");
    return -1;
  }
  return 0;
}

static int run_term(const char *reply, char **cmd) {
  int master, slave;
  char name[256];
  pid_t pid;
  struct termios t;
  char transcript[TRANSCRIPT_MAX];
  size_t len = 0;
  int answered = 0;
  int status = 0;
  int child_done = 0;
  double deadline = now() + DEADLINE_SECONDS;

  if (open_pair(&master, &slave, name, sizeof(name)) < 0) {
    fprintf(stderr, "inject_probe: cannot open a pseudo-terminal: %s\n", strerror(errno));
    return 2;
  }
  /* Canonical mode, echo off, CR mapped to NL on input (the default ICRNL). */
  if (tcgetattr(slave, &t) < 0) {
    fprintf(stderr, "inject_probe: tcgetattr: %s\n", strerror(errno));
    return 2;
  }
  t.c_lflag |= ICANON;
  t.c_lflag &= ~(tcflag_t)ECHO;
  t.c_iflag |= ICRNL;
  t.c_oflag |= OPOST | ONLCR;
  if (tcsetattr(slave, TCSANOW, &t) < 0) {
    fprintf(stderr, "inject_probe: tcsetattr: %s\n", strerror(errno));
    return 2;
  }

  pid = fork();
  if (pid < 0) {
    return 2;
  }
  if (pid == 0) {
    int fd;
    close(master);
    if (setsid() < 0) {
      _exit(125);
    }
    fd = open(name, O_RDWR);
    if (fd < 0 || ioctl(fd, TIOCSCTTY, 0) < 0) {
      _exit(125);
    }
    close(slave);
    if (dup2(fd, 0) < 0 || dup2(fd, 1) < 0 || dup2(fd, 2) < 0) {
      _exit(125);
    }
    if (fd > 2) {
      close(fd);
    }
    execvp(cmd[0], cmd);
    _exit(127);
  }
  close(slave);

  while (now() < deadline && !child_done) {
    struct pollfd pfd;
    ssize_t n;
    pfd.fd = master;
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, 200) > 0) {
      n = read(master, transcript + len, sizeof(transcript) - 1 - len);
      if (n <= 0) {
        child_done = 1;
      } else {
        len += (size_t)n;
        transcript[len] = '\0';
      }
    }
    /* When the child asks for its title, answer with the injected line(s). */
    if (reply != NULL && !answered && find_bytes(transcript, len, "\033[21t") != NULL) {
      if (send_reply(master, reply, strlen(reply)) < 0) {
        break;
      }
      answered = 1;
    }
    if (len >= sizeof(transcript) - 1) {
      break;
    }
  }

  {
    double reap_by = now() + 5.0;
    pid_t r = 0;
    while (child_done && now() < reap_by) {
      r = waitpid(pid, &status, WNOHANG);
      if (r != 0) {
        break;
      }
      usleep(20000);
    }
    if (r <= 0) {
      kill(pid, SIGKILL);
      if (waitpid(pid, &status, 0) < 0) {
        status = 0;
      }
      printf("deadline=hit\n");
    }
  }
  fputs("transcript=", stdout);
  print_escaped(transcript, len);
  putchar('\n');
  if (WIFEXITED(status)) {
    printf("child_status=%d\n", WEXITSTATUS(status));
  } else {
    printf("child_status=signal-%d\n", WIFSIGNALED(status) ? WTERMSIG(status) : -1);
  }
  close(master);
  return 0;
}

int main(int argc, char **argv) {
  if (argc >= 3 && strcmp(argv[1], "term") == 0) {
    return run_term("h4ck\r", argv + 2);
  }
  if (argc >= 3 && strcmp(argv[1], "term2") == 0) {
    return run_term("a\rb\r", argv + 2);
  }
  if (argc >= 3 && strcmp(argv[1], "show") == 0) {
    return run_term(NULL, argv + 2);
  }
  fprintf(stderr, "usage: inject_probe term|term2|show CMD [ARG...]\n");
  return 2;
}
