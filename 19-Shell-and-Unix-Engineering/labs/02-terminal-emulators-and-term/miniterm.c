/*
 * miniterm: a terminal emulator with no screen.
 *
 *   miniterm [-m] [-t TEXT] ROWS COLS [NAME=VALUE...] -- CMD [ARG...]
 *
 * It does the four things every terminal emulator does for the program it hosts:
 *   1. allocates a pseudo-terminal and keeps the master side;
 *   2. stores the window size in the kernel with TIOCSWINSZ (ROWS by COLS);
 *   3. composes the child's environment: the child gets exactly the NAME=VALUE
 *      words given here and nothing else, so TERM exists only if miniterm exports it;
 *   4. reads every byte the child writes and reacts to control sequences:
 *        ESC [ c  or  ESC [ 0 c     Primary Device Attributes -> reply ESC [ ? 6 2 ; 2 2 c
 *        ESC [ 8 ; R ; C t          xterm window operation 8 -> TIOCSWINSZ to R rows, C columns
 *      Replies are written to the master, so the child reads them as if typed.
 *
 *   -m       mute: parse but never reply to Device Attributes (a terminal that cannot answer)
 *   -t TEXT  once the child has written something and then been silent for
 *            QUIET_SECONDS, type TEXT followed by a carriage return; type it again
 *            after every further RETYPE_SECONDS of silence, in case a line editor
 *            that was still starting up discarded the first copy
 *
 * Output: "screen=" and every byte the child wrote (ESC as <ESC>, CR as \r, LF as \n,
 * other control bytes as \xNN), then "status=" and the child's exit status.
 * Every wait is bounded: at DEADLINE_SECONDS the child's process group is killed and
 * "deadline=hit" is printed.
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

#define SCREEN_MAX 65536
#define PARAM_MAX 64
#define DEADLINE_SECONDS 20.0
#define QUIET_SECONDS 1.5
#define RETYPE_SECONDS 5.0

extern char **environ;

static const char da_reply[] = "\033[?62;22c";

enum state { GROUND, ESCAPE, CSI };

struct term {
  int master;
  int mute;
  enum state state;
  char param[PARAM_MAX];
  size_t param_len;
};

static double now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

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
    } else if (c < 0x20 || c >= 0x7F) {
      printf("\\x%02X", c);
    } else {
      putchar(c);
    }
  }
}

static int set_size(int fd, int rows, int cols) {
  struct winsize ws;
  memset(&ws, 0, sizeof(ws));
  ws.ws_row = (unsigned short)rows;
  ws.ws_col = (unsigned short)cols;
  return ioctl(fd, TIOCSWINSZ, &ws);
}

/* A complete control sequence ESC [ PARAM FINAL has arrived: act on the two this terminal knows. */
static void dispatch(struct term *t, char final) {
  int rows, cols;
  t->param[t->param_len] = '\0';
  if (final == 'c' && (t->param_len == 0 || strcmp(t->param, "0") == 0)) {
    if (!t->mute && write(t->master, da_reply, sizeof(da_reply) - 1) < 0) {
      perror("miniterm: reply");
    }
  } else if (final == 't' && sscanf(t->param, "8;%d;%d", &rows, &cols) == 2) {
    if (set_size(t->master, rows, cols) < 0) {
      perror("miniterm: TIOCSWINSZ");
    }
  }
}

/* The parser: ground, ESC seen, inside a control sequence (parameter bytes until a final byte). */
static void feed(struct term *t, const char *buf, size_t len) {
  size_t i;
  for (i = 0; i < len; i++) {
    unsigned char c = (unsigned char)buf[i];
    switch (t->state) {
      case GROUND:
        if (c == 0x1B) {
          t->state = ESCAPE;
        }
        break;
      case ESCAPE:
        if (c == '[') {
          t->state = CSI;
          t->param_len = 0;
        } else if (c != 0x1B) {
          t->state = GROUND;
        }
        break;
      case CSI:
        if (c >= 0x40 && c <= 0x7E) {
          dispatch(t, (char)c);
          t->state = GROUND;
        } else if (c == 0x1B) {
          t->state = ESCAPE;
        } else if (t->param_len < PARAM_MAX - 1) {
          t->param[t->param_len++] = (char)c;
        }
        break;
      default:
        t->state = GROUND;
        break;
    }
  }
}

static int usage(void) {
  fputs("usage: miniterm [-m] [-t TEXT] ROWS COLS [NAME=VALUE...] -- CMD [ARG...]\n", stderr);
  return 2;
}

int main(int argc, char **argv) {
  struct term t;
  const char *type_text = NULL;
  const char *sname;
  char name[256];
  static char screen[SCREEN_MAX];
  size_t len = 0;
  int rows, cols, first_env, cmd_at, i, slave;
  int typed = 0, eof = 0, status = 0, hit = 0;
  double deadline, last_output = 0.0;
  pid_t pid, r = 0;

  memset(&t, 0, sizeof(t));
  t.state = GROUND;
  i = 1;
  while (i < argc && argv[i][0] == '-' && argv[i][1] != '\0' && strcmp(argv[i], "--") != 0) {
    if (strcmp(argv[i], "-m") == 0) {
      t.mute = 1;
      i++;
    } else if (strcmp(argv[i], "-t") == 0 && i + 1 < argc) {
      type_text = argv[i + 1];
      i += 2;
    } else {
      return usage();
    }
  }
  if (argc - i < 4) {
    return usage();
  }
  rows = atoi(argv[i]);
  cols = atoi(argv[i + 1]);
  first_env = i + 2;
  cmd_at = first_env;
  while (cmd_at < argc && strcmp(argv[cmd_at], "--") != 0) {
    if (strchr(argv[cmd_at], '=') == NULL) {
      return usage();
    }
    cmd_at++;
  }
  if (rows <= 0 || cols <= 0 || cmd_at + 1 >= argc) {
    return usage();
  }
  argv[cmd_at] = NULL; /* argv[first_env .. cmd_at) is now a NULL-terminated environment */
  cmd_at++;

  t.master = posix_openpt(O_RDWR | O_NOCTTY);
  if (t.master < 0 || grantpt(t.master) < 0 || unlockpt(t.master) < 0) {
    perror("miniterm: cannot open a pseudo-terminal");
    return 2;
  }
  sname = ptsname(t.master);
  if (sname == NULL) {
    perror("miniterm: ptsname");
    return 2;
  }
  snprintf(name, sizeof(name), "%s", sname);
  slave = open(name, O_RDWR | O_NOCTTY);
  if (slave < 0 || set_size(slave, rows, cols) < 0) {
    perror("miniterm: cannot size the terminal");
    return 2;
  }

  pid = fork();
  if (pid < 0) {
    perror("miniterm: fork");
    return 2;
  }
  if (pid == 0) {
    int fd;
    close(t.master);
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
    environ = argv + first_env;
    execvp(argv[cmd_at], argv + cmd_at);
    _exit(127);
  }
  close(slave);

  deadline = now() + DEADLINE_SECONDS;
  while (!eof && now() < deadline && len < sizeof(screen)) {
    struct pollfd pfd;
    pfd.fd = t.master;
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, 100) > 0) {
      ssize_t n = read(t.master, screen + len, sizeof(screen) - len);
      if (n <= 0) {
        eof = 1; /* 0 on macOS, EIO on Linux: the last slave descriptor closed */
      } else {
        feed(&t, screen + len, (size_t)n);
        len += (size_t)n;
        last_output = now();
      }
    } else if (type_text != NULL && len > 0 && now() - last_output >= (typed ? RETYPE_SECONDS : QUIET_SECONDS)) {
      if (write(t.master, type_text, strlen(type_text)) < 0 || write(t.master, "\r", 1) < 0) {
        perror("miniterm: type");
      }
      typed = 1;
      last_output = now();
    }
  }

  if (eof) {
    double reap_by = now() + 5.0;
    while (now() < reap_by) {
      r = waitpid(pid, &status, WNOHANG);
      if (r != 0) {
        break;
      }
      usleep(20000);
    }
  }
  if (r <= 0) {
    /* Deadline, full buffer, or a child that will not exit: kill its whole session. */
    kill(-pid, SIGKILL);
    kill(pid, SIGKILL);
    if (waitpid(pid, &status, 0) < 0) {
      status = 0;
    }
    hit = 1;
  }
  close(t.master);

  if (hit) {
    puts("deadline=hit");
  }
  fputs("screen=", stdout);
  print_escaped(screen, len);
  putchar('\n');
  if (WIFEXITED(status)) {
    printf("status=%d\n", WEXITSTATUS(status));
  } else {
    printf("status=signal-%d\n", WIFSIGNALED(status) ? WTERMSIG(status) : -1);
  }
  return 0;
}
