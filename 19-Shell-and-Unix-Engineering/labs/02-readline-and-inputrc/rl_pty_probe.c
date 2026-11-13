/*
 * rl_pty_probe: drive an interactive shell on a pseudo-terminal the way a terminal emulator does,
 * and report what its line editor did.
 *
 *   rl_pty_probe keys k:KEYS... m:MARKER... -- SHELL [ARG...]
 *       type each KEYS chunk one second apart, then print MARKER=1 when MARKER appeared in the
 *       shell's output after the first chunk was typed, MARKER=0 when it did not
 *   rl_pty_probe termios -- SHELL [ARG...]
 *       print the terminal modes while the shell waits at its prompt and again while a
 *       foreground /bin/sleep runs
 *   rl_pty_probe paste -- SHELL [ARG...]
 *       paste two command lines as a terminal would: between ESC[200~ and ESC[201~ when the
 *       shell switched bracketed paste on (ESC[?2004h), bare otherwise; count how many of the
 *       two commands ran before and after Enter was pressed
 *
 * KEYS understands \r, \n, \e, \\ and \xHH. Every marker a test looks for is the result of an
 * arithmetic expansion, so the echo of the typed text can never match it: only a command the
 * shell really executed prints it.
 *
 * The child shell is the leader of a new session whose controlling terminal is the slave side.
 * Every wait is bounded, and the shell's process groups are killed before the probe returns.
 */
#define _XOPEN_SOURCE 700
#define _DEFAULT_SOURCE
#define _DARWIN_C_SOURCE

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

#define OUT_CAP (1 << 20)
#define MAX_ITEMS 16

static int master_fd = -1;
static int slave_fd = -1;
static pid_t child = -1;
static char out[OUT_CAP];
static size_t out_len = 0;

static long now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void nap_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}

/* Collect what the shell writes for ms milliseconds; NUL bytes become spaces so strstr works. */
static void pump(long ms) {
  long end = now_ms() + ms;
  for (;;) {
    long left = end - now_ms();
    if (left <= 0) {
      return;
    }
    struct pollfd pfd;
    pfd.fd = master_fd;
    pfd.events = POLLIN;
    pfd.revents = 0;
    int r = poll(&pfd, 1, (int)left);
    if (r < 0 && errno != EINTR) {
      return;
    }
    if (r <= 0) {
      continue;
    }
    char buf[4096];
    ssize_t n = read(master_fd, buf, sizeof(buf));
    if (n <= 0) {
      nap_ms(10); /* hangup or error: keep the deadline, never spin */
      continue;
    }
    for (ssize_t i = 0; i < n && out_len < OUT_CAP - 1; i++) {
      out[out_len++] = buf[i] == '\0' ? ' ' : buf[i];
    }
    out[out_len] = '\0';
  }
}

/* 1 when needle shows up in the output at or after offset from within timeout_ms. */
static int wait_for(const char *needle, size_t from, long timeout_ms) {
  long end = now_ms() + timeout_ms;
  for (;;) {
    if (strstr(out + from, needle) != NULL) {
      return 1;
    }
    if (now_ms() >= end) {
      return 0;
    }
    pump(50);
  }
}

static int send_bytes(const char *buf, size_t len) {
  size_t done = 0;
  while (done < len) {
    ssize_t n = write(master_fd, buf + done, len - done);
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      return -1;
    }
    done += (size_t)n;
  }
  return 0;
}

static int send_str(const char *s) {
  return send_bytes(s, strlen(s));
}

static int hex_value(char c) {
  if (c >= '0' && c <= '9') {
    return c - '0';
  }
  if (c >= 'a' && c <= 'f') {
    return c - 'a' + 10;
  }
  if (c >= 'A' && c <= 'F') {
    return c - 'A' + 10;
  }
  return -1;
}

/* Decode \r, \n, \e, \\ and \xHH in place; returns the decoded length, or -1 on a bad escape. */
static long decode_keys(char *s) {
  size_t r = 0;
  size_t w = 0;
  while (s[r] != '\0') {
    if (s[r] != '\\') {
      s[w++] = s[r++];
      continue;
    }
    char e = s[r + 1];
    if (e == 'r') {
      s[w++] = '\r';
      r += 2;
    } else if (e == 'n') {
      s[w++] = '\n';
      r += 2;
    } else if (e == 'e') {
      s[w++] = '\033';
      r += 2;
    } else if (e == '\\') {
      s[w++] = '\\';
      r += 2;
    } else if (e == 'x' && hex_value(s[r + 2]) >= 0 && hex_value(s[r + 3]) >= 0) {
      s[w++] = (char)(hex_value(s[r + 2]) * 16 + hex_value(s[r + 3]));
      r += 4;
    } else {
      return -1;
    }
  }
  return (long)w;
}

static void kill_session(void) {
  if (child <= 0) {
    return;
  }
  pid_t fg = tcgetpgrp(master_fd);
  if (fg > 1 && fg != child) {
    kill(-fg, SIGKILL);
  }
  kill(-child, SIGKILL);
  kill(child, SIGKILL);
  waitpid(child, NULL, 0);
  child = -1;
}

/* Start SHELL on a fresh pseudo-terminal sized 24x80, as the leader of its own session. */
static int spawn_shell(char **cmd) {
  master_fd = posix_openpt(O_RDWR | O_NOCTTY);
  if (master_fd < 0 || grantpt(master_fd) < 0 || unlockpt(master_fd) < 0) {
    perror("posix_openpt");
    return -1;
  }
  const char *name = ptsname(master_fd);
  if (name == NULL) {
    perror("ptsname");
    return -1;
  }
  /* The parent keeps its own slave descriptor so it can read the terminal modes at any time. */
  slave_fd = open(name, O_RDWR | O_NOCTTY);
  if (slave_fd < 0) {
    perror("open slave");
    return -1;
  }
  struct winsize ws;
  memset(&ws, 0, sizeof(ws));
  ws.ws_row = 24;
  ws.ws_col = 80;
  if (ioctl(slave_fd, TIOCSWINSZ, &ws) < 0) {
    perror("TIOCSWINSZ");
    return -1;
  }
  child = fork();
  if (child < 0) {
    perror("fork");
    return -1;
  }
  if (child == 0) {
    sigset_t none;
    sigemptyset(&none);
    sigprocmask(SIG_SETMASK, &none, NULL);
    int sigs[] = {SIGINT, SIGQUIT, SIGTSTP, SIGTTIN, SIGTTOU, SIGHUP, SIGPIPE, SIGCHLD, SIGTERM};
    for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); i++) {
      signal(sigs[i], SIG_DFL);
    }
    close(master_fd);
    close(slave_fd);
    if (setsid() < 0) {
      _exit(126);
    }
    int fd = open(name, O_RDWR);
    if (fd < 0 || ioctl(fd, TIOCSCTTY, 0) < 0) {
      _exit(126);
    }
    dup2(fd, STDIN_FILENO);
    dup2(fd, STDOUT_FILENO);
    dup2(fd, STDERR_FILENO);
    if (fd > STDERR_FILENO) {
      close(fd);
    }
    execvp(cmd[0], cmd);
    _exit(127);
  }
  return 0;
}

/*
 * Wait for the first prompt, then type a command whose output proves the shell reads and runs
 * what arrives on the terminal. Returns 1 when the shell is ready.
 */
static int handshake(void) {
  long end = now_ms() + 10000;
  while (out_len == 0 && now_ms() < end) {
    pump(50);
  }
  pump(300);
  if (send_str("echo R$((3+4))Y\r") < 0) {
    return 0;
  }
  if (!wait_for("R7Y", 0, 10000)) {
    return 0;
  }
  pump(1000); /* let the editor print the next prompt and take the terminal */
  return 1;
}

/* Submit whatever is on the line, then run one more computed command: 1 when the shell answers. */
static int still_alive(void) {
  size_t from;
  if (send_str("\r") < 0) {
    return 0;
  }
  pump(500);
  from = out_len;
  if (send_str("echo E$((5+4))D\r") < 0) {
    return 0;
  }
  return wait_for("E9D", from, 15000);
}

static void dump_transcript(void) {
  int status = 0;
  if (child > 0 && waitpid(child, &status, WNOHANG) == child) {
    fprintf(stderr, "the shell exited early, wait status 0x%x (exit 127: exec failed)\n", (unsigned)status);
    child = -1;
  }
  fprintf(stderr, "transcript (%lu bytes):\n", (unsigned long)out_len);
  for (size_t i = 0; i < out_len; i++) {
    unsigned char c = (unsigned char)out[i];
    if (c == '\n' || (c >= 0x20 && c < 0x7f)) {
      fputc(c, stderr);
    } else {
      fprintf(stderr, "\\x%02x", c);
    }
  }
  fputc('\n', stderr);
}

static void sample_termios(const char *label) {
  struct termios tio;
  if (tcgetattr(slave_fd, &tio) < 0) {
    printf("%s: tcgetattr failed\n", label);
    return;
  }
  printf("%s: icanon=%d echo=%d isig=%d icrnl=%d vmin=%d vtime=%d\n", label,
         (tio.c_lflag & ICANON) != 0, (tio.c_lflag & ECHO) != 0, (tio.c_lflag & ISIG) != 0,
         (tio.c_iflag & ICRNL) != 0, (int)tio.c_cc[VMIN], (int)tio.c_cc[VTIME]);
}

/* Offset just past the last occurrence of needle in the output, or 0 when it never appeared. */
static size_t last_seen(const char *needle) {
  size_t pos = 0;
  const char *p = out;
  const char *hit;
  while ((hit = strstr(p, needle)) != NULL) {
    pos = (size_t)(hit - out) + 1;
    p = hit + 1;
  }
  return pos;
}

static int run_keys(int nkeys, char **keys, long *key_len, int nmarkers, char **markers) {
  size_t mark = out_len;
  for (int i = 0; i < nkeys; i++) {
    if (send_bytes(keys[i], (size_t)key_len[i]) < 0) {
      return 3;
    }
    pump(1000);
  }
  pump(1000);
  printf("ready=1");
  for (int i = 0; i < nmarkers; i++) {
    printf(" %s=%d", markers[i], strstr(out + mark, markers[i]) != NULL);
  }
  printf(" alive=%d\n", still_alive());
  return 0;
}

static int run_termios(void) {
  size_t from;
  int alive;
  printf("ready=1\n");
  sample_termios("prompt");
  if (send_str("/bin/sleep 3\r") < 0) {
    return 3;
  }
  pump(1000);
  sample_termios("command");
  from = out_len;
  if (send_str("echo E$((5+4))D\r") < 0) {
    return 3;
  }
  alive = wait_for("E9D", from, 15000);
  printf("alive=%d\n", alive);
  return 0;
}

static int count_pasted(size_t mark) {
  return (strstr(out + mark, "P2A") != NULL) + (strstr(out + mark, "P4B") != NULL);
}

static int run_paste(void) {
  static const char text[] = "echo P$((1+1))A\recho P$((2+2))B\r";
  int bracketed = last_seen("\033[?2004h") > last_seen("\033[?2004l");
  size_t mark = out_len;
  int before;
  int after;
  if (bracketed && send_str("\033[200~") < 0) {
    return 3;
  }
  if (send_str(text) < 0) {
    return 3;
  }
  if (bracketed && send_str("\033[201~") < 0) {
    return 3;
  }
  pump(2000);
  before = count_pasted(mark);
  if (send_str("\r") < 0) {
    return 3;
  }
  pump(2000);
  after = count_pasted(mark);
  printf("ready=1 mode2004=%d before_enter=%d after_enter=%d alive=%d\n", bracketed, before, after,
         still_alive());
  return 0;
}

static int usage(const char *argv0) {
  fprintf(stderr, "usage: %s keys k:KEYS... m:MARKER... -- SHELL [ARG...]\n", argv0);
  fprintf(stderr, "       %s termios -- SHELL [ARG...]\n", argv0);
  fprintf(stderr, "       %s paste -- SHELL [ARG...]\n", argv0);
  return 2;
}

int main(int argc, char **argv) {
  char *keys[MAX_ITEMS];
  long key_len[MAX_ITEMS];
  char *markers[MAX_ITEMS];
  int nkeys = 0;
  int nmarkers = 0;
  int i;
  int rc;

  if (argc < 4) {
    return usage(argv[0]);
  }
  const char *mode = argv[1];
  for (i = 2; i < argc && strcmp(argv[i], "--") != 0; i++) {
    if (strncmp(argv[i], "k:", 2) == 0 && nkeys < MAX_ITEMS) {
      keys[nkeys] = argv[i] + 2;
      key_len[nkeys] = decode_keys(keys[nkeys]);
      if (key_len[nkeys] < 0) {
        fprintf(stderr, "bad escape in %s\n", argv[i]);
        return 2;
      }
      nkeys++;
    } else if (strncmp(argv[i], "m:", 2) == 0 && nmarkers < MAX_ITEMS) {
      markers[nmarkers++] = argv[i] + 2;
    } else {
      return usage(argv[0]);
    }
  }
  if (i >= argc - 1) {
    return usage(argv[0]);
  }
  int is_keys = strcmp(mode, "keys") == 0;
  int is_termios = strcmp(mode, "termios") == 0;
  int is_paste = strcmp(mode, "paste") == 0;
  if ((!is_keys && !is_termios && !is_paste) || (is_keys && (nkeys == 0 || nmarkers == 0))
      || (!is_keys && (nkeys != 0 || nmarkers != 0))) {
    return usage(argv[0]);
  }

  setvbuf(stdout, NULL, _IOLBF, 0);
  if (spawn_shell(&argv[i + 1]) < 0) {
    return 3;
  }
  if (!handshake()) {
    printf("ready=0\n");
    dump_transcript();
    kill_session();
    return 1;
  }
  if (is_keys) {
    rc = run_keys(nkeys, keys, key_len, nmarkers, markers);
  } else if (is_termios) {
    rc = run_termios();
  } else {
    rc = run_paste();
  }
  kill_session();
  return rc;
}
