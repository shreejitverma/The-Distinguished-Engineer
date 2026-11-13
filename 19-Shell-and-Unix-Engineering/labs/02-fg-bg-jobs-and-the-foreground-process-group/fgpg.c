/*
 * fgpg: probes for the fg, bg, jobs and foreground process group lab.
 *
 *   fgpg pty OUTDIR [--type MARKER=TEXT | --key MARKER=OCTALS]... --shell WORDS -- [ARG...]
 *       run WORDS ARG... in OUTDIR as the leader of a new session whose controlling terminal is a
 *       fresh pseudo-terminal, and play the user at its master end, one step after another:
 *       --type waits for the file OUTDIR/MARKER (when MARKER is not empty) and for a prompt printed
 *       since the previous step, then types TEXT and a newline; --key waits for OUTDIR/MARKER, then
 *       types the bytes OCTALS (for example \032 for the SUSP character);
 *       everything the terminal prints goes to OUTDIR/tty.out, and the shell's exit is reported
 *   fgpg job LOGDIR NAME START EXIT [MAXMS]
 *       a job that records its own role: wait until it is in the START (fg or bg) role, publish
 *       NAME.pid and NAME.ready, log every change of role and every SIGCONT with the role it had
 *       when the signal arrived, and exit with status EXIT once it reaches the foreground after
 *       having been in the background (or after MAXMS milliseconds, with status 99)
 *   fgpg hold LOGDIR NAME      publish NAME.pid and NAME.ready, then sleep until killed
 *   fgpg who SHELLPID LOGDIR [NAME...]
 *       say which group owns the terminal now: the shell's, job NAME's (from NAME.pid), or another
 *   fgpg ttymode LOGDIR NAME   clear ECHO, publish NAME.ready, and log ECHO as seen at each SIGCONT
 *   fgpg echoflag              print echo=on or echo=off for the controlling terminal
 *   fgpg ttyerr                print what tcsetpgrp returns for three kinds of process group ID
 *   fgpg state PID             print R, S, T, Z, or ? for the process state; fail for Z and ?
 *   fgpg pgid PID              print the process group ID of a process
 *   fgpg tstp                  print the number of SIGTSTP on this system
 *   fgpg msleep MS             sleep
 *
 * NAME.pid holds "PID PGID". Every mode that can wait arms an alarm first, so nothing outlives a
 * failed test.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

#define MAX_WORDS 16
#define MAX_STEPS 12
#define PROMPT "@@"
#define MAX_ARGV 64

static volatile sig_atomic_t g_conts;
static volatile sig_atomic_t g_write_failed;
static int g_log_fd = -1;
static int g_tty_fd = -1;
static int g_log_echo;

static long mono_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long)ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void sleep_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}

static int file_exists(const char *path) {
  struct stat st;
  return stat(path, &st) == 0;
}

static int touch(const char *path) {
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0) {
    return -1;
  }
  close(fd);
  return 0;
}

static int write_file(const char *path, const char *text) {
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  ssize_t n;
  if (fd < 0) {
    return -1;
  }
  n = write(fd, text, strlen(text));
  close(fd);
  return n < 0 ? -1 : 0;
}

/* Publish LOGDIR/NAME.pid ("PID PGID") and then LOGDIR/NAME.ready. */
static int publish(const char *dir, const char *name) {
  char path[512];
  char text[64];
  snprintf(path, sizeof(path), "%s/%s.pid", dir, name);
  snprintf(text, sizeof(text), "%ld %ld\n", (long)getpid(), (long)getpgrp());
  if (write_file(path, text) < 0) {
    return -1;
  }
  snprintf(path, sizeof(path), "%s/%s.ready", dir, name);
  return touch(path);
}

static int open_log(const char *path) {
  return open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND, 0644);
}

static const char *err_name(int err) {
  switch (err) {
  case 0:
    return "ok";
  case EPERM:
    return "EPERM";
  case ESRCH:
    return "ESRCH";
  case EINVAL:
    return "EINVAL";
  case ENOTTY:
    return "ENOTTY";
  case EIO:
    return "EIO";
  default:
    return "E?";
  }
}

/* 1 when this process is in the foreground process group of its controlling terminal. */
static int in_foreground(void) {
  return tcgetpgrp(g_tty_fd) == getpgrp();
}

/* SIGCONT handler: record the role this process has at the moment the signal is delivered.
 * tcgetpgrp, getpgrp, tcgetattr and write are async-signal-safe (POSIX.1-2024 XSH 2.4.3). */
static void on_cont(int sig) {
  int saved = errno;
  char buf[48];
  size_t n = 0;
  const char *role = in_foreground() ? "cont role=fg" : "cont role=bg";
  size_t len = strlen(role);
  (void)sig;
  memcpy(buf, role, len);
  n = len;
  if (g_log_echo) {
    struct termios t;
    const char *echo = " echo=?";
    if (tcgetattr(g_tty_fd, &t) == 0) {
      echo = (t.c_lflag & ECHO) ? " echo=on" : " echo=off";
    }
    len = strlen(echo);
    memcpy(buf + n, echo, len);
    n += len;
  }
  buf[n++] = '\n';
  if (write(g_log_fd, buf, n) < 0) {
    g_write_failed = 1;
  }
  g_conts = g_conts + 1;
  errno = saved;
}

static void catch_cont(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_cont;
  sigemptyset(&sa.sa_mask);
  sigaction(SIGCONT, &sa, NULL);
}

/* Wait until this process is (want_fg) or is not (!want_fg) in the terminal's foreground group. */
static int wait_role(int want_fg, long timeout_ms) {
  long deadline = mono_ms() + timeout_ms;
  for (;;) {
    pid_t tp = tcgetpgrp(g_tty_fd);
    if (tp > 0 && (tp == getpgrp()) == want_fg) {
      return 1;
    }
    if (mono_ms() > deadline) {
      return 0;
    }
    sleep_ms(20);
  }
}

static int cmd_job(int argc, char **argv) {
  char path[512];
  int want_fg;
  int exit_code;
  int role;
  int was_bg;
  long max_ms = 20000;
  long deadline;
  if (argc != 6 && argc != 7) {
    return 2;
  }
  alarm(25);
  want_fg = strcmp(argv[4], "fg") == 0;
  exit_code = atoi(argv[5]);
  if (argc == 7) {
    max_ms = strtol(argv[6], NULL, 10);
  }
  snprintf(path, sizeof(path), "%s/%s.log", argv[2], argv[3]);
  g_log_fd = open_log(path);
  g_tty_fd = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (g_log_fd < 0 || g_tty_fd < 0) {
    return 3;
  }
  catch_cont();
  if (!wait_role(want_fg, 5000)) {
    dprintf(g_log_fd, "notready role=%s\n", argv[4]);
    return 4;
  }
  role = want_fg;
  was_bg = !want_fg;
  dprintf(g_log_fd, "start role=%s leader=%s\n", argv[4], getpgrp() == getpid() ? "yes" : "no");
  if (publish(argv[2], argv[3]) < 0) {
    return 3;
  }
  deadline = mono_ms() + max_ms;
  for (;;) {
    int now_fg = in_foreground();
    if (now_fg != role) {
      role = now_fg;
      dprintf(g_log_fd, "role %s\n", role ? "fg" : "bg");
    }
    if (!role) {
      was_bg = 1;
    } else if (was_bg) {
      dprintf(g_log_fd, "exit write_failed=%d\n", (int)g_write_failed);
      return exit_code;
    }
    if (mono_ms() > deadline) {
      dprintf(g_log_fd, "timeout role=%s\n", role ? "fg" : "bg");
      return 99;
    }
    sleep_ms(20);
  }
}

static int cmd_hold(int argc, char **argv) {
  if (argc != 4) {
    return 2;
  }
  alarm(25);
  if (publish(argv[2], argv[3]) < 0) {
    return 3;
  }
  for (;;) {
    sleep_ms(100);
  }
}

static int cmd_who(int argc, char **argv) {
  pid_t shell;
  pid_t tp;
  int i;
  if (argc < 4) {
    return 2;
  }
  alarm(25);
  shell = (pid_t)strtol(argv[2], NULL, 10);
  g_tty_fd = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (g_tty_fd < 0) {
    printf("tty_fg=notty\n");
    return 0;
  }
  tp = tcgetpgrp(g_tty_fd);
  if (tp == getpgid(shell)) {
    printf("tty_fg=shell\n");
    return 0;
  }
  for (i = 4; i < argc; i++) {
    char path[512];
    long pid = -1;
    long pgrp = -1;
    FILE *f;
    snprintf(path, sizeof(path), "%s/%s.pid", argv[3], argv[i]);
    f = fopen(path, "r");
    if (f != NULL) {
      if (fscanf(f, "%ld %ld", &pid, &pgrp) != 2) {
        pgrp = -1;
      }
      fclose(f);
    }
    if (pgrp == (long)tp) {
      printf("tty_fg=job:%s\n", argv[i]);
      return 0;
    }
  }
  printf("tty_fg=%s\n", tp == getpgrp() ? "self" : "other");
  return 0;
}

static int cmd_ttymode(int argc, char **argv) {
  char path[512];
  struct termios saved;
  struct termios t;
  long deadline;
  if (argc != 4) {
    return 2;
  }
  alarm(25);
  snprintf(path, sizeof(path), "%s/%s.log", argv[2], argv[3]);
  g_log_fd = open_log(path);
  g_tty_fd = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (g_log_fd < 0 || g_tty_fd < 0 || !wait_role(1, 5000)) {
    return 4;
  }
  if (tcgetattr(g_tty_fd, &saved) < 0) {
    return 5;
  }
  t = saved;
  t.c_lflag &= ~(tcflag_t)ECHO;
  if (tcsetattr(g_tty_fd, TCSANOW, &t) < 0) {
    return 5;
  }
  dprintf(g_log_fd, "set echo=off was=%s\n", (saved.c_lflag & ECHO) ? "on" : "off");
  g_log_echo = 1;
  catch_cont();
  if (publish(argv[2], argv[3]) < 0) {
    return 3;
  }
  /* The harness types the SUSP character now; the default action of SIGTSTP stops this process. */
  deadline = mono_ms() + 20000;
  while (g_conts == 0 && mono_ms() < deadline) {
    sleep_ms(20);
  }
  if (wait_role(1, 3000)) {
    /* Leave the terminal as this job found it; only a foreground process may do that quietly. */
    tcsetattr(g_tty_fd, TCSANOW, &saved);
  }
  dprintf(g_log_fd, "exit conts=%d write_failed=%d\n", (int)g_conts, (int)g_write_failed);
  return 0;
}

static int cmd_echoflag(void) {
  struct termios t;
  int tty = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (tty < 0 || tcgetattr(tty, &t) < 0) {
    printf("echo=?\n");
    return 0;
  }
  printf("echo=%s\n", (t.c_lflag & ECHO) ? "on" : "off");
  return 0;
}

static int cmd_state(int argc, char **argv) {
  long pid;
  char st = '?';
  if (argc != 3) {
    return 2;
  }
  pid = strtol(argv[2], NULL, 10);
#ifdef __APPLE__
  {
    int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, (int)pid};
    struct kinfo_proc kp;
    size_t len = sizeof(kp);
    memset(&kp, 0, sizeof(kp));
    if (sysctl(mib, 4, &kp, &len, NULL, 0) == 0 && len > 0) {
      switch (kp.kp_proc.p_stat) {
      case SSTOP:
        st = 'T';
        break;
      case SZOMB:
        st = 'Z';
        break;
      case SRUN:
        st = 'R';
        break;
      default:
        st = 'S';
        break;
      }
    }
  }
#else
  {
    char path[64];
    char buf[512];
    FILE *f;
    char *p;
    snprintf(path, sizeof(path), "/proc/%ld/stat", pid);
    f = fopen(path, "r");
    if (f != NULL) {
      size_t n = fread(buf, 1, sizeof(buf) - 1, f);
      fclose(f);
      buf[n] = '\0';
      p = strrchr(buf, ')');
      if (p != NULL && p[1] == ' ' && p[2] != '\0') {
        st = p[2];
      }
    }
  }
#endif
  printf("%c\n", st);
  return st == '?' || st == 'Z';
}

/* What tcsetpgrp answers a session leader that owns the terminal, for its own group, for a process
 * group ID that names no process group, and for a process group of another session. */
static int cmd_ttyerr(void) {
  int master;
  pid_t child;
  int status = 0;
  alarm(25);
  master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt(master) < 0 || unlockpt(master) < 0) {
    perror("pty");
    return 3;
  }
  child = fork();
  if (child < 0) {
    perror("fork");
    return 3;
  }
  if (child == 0) {
    int slave;
    int pfd[2];
    pid_t gone;
    pid_t other;
    char c;
    int e_own;
    int e_gone;
    int e_other;
    if (setsid() < 0) {
      _exit(126);
    }
    slave = open(ptsname(master), O_RDWR);
    if (slave < 0 || ioctl(slave, TIOCSCTTY, 0) < 0) {
      _exit(126);
    }
    e_own = tcsetpgrp(slave, getpgrp()) < 0 ? errno : 0;
    /* A process ID that was just reaped names no process and no process group. */
    gone = fork();
    if (gone == 0) {
      _exit(0);
    }
    waitpid(gone, NULL, 0);
    e_gone = tcsetpgrp(slave, gone) < 0 ? errno : 0;
    /* A child that calls setsid leads a process group in another session. */
    if (pipe(pfd) < 0) {
      _exit(126);
    }
    other = fork();
    if (other == 0) {
      alarm(20);
      close(pfd[0]);
      setsid();
      if (write(pfd[1], "s", 1) < 0) {
        _exit(126);
      }
      for (;;) {
        sleep_ms(100);
      }
    }
    close(pfd[1]);
    if (read(pfd[0], &c, 1) != 1) {
      _exit(126);
    }
    e_other = tcsetpgrp(slave, other) < 0 ? errno : 0;
    kill(other, SIGKILL);
    waitpid(other, NULL, 0);
    printf("own_group=%s no_such_group=%s other_session=%s\n", err_name(e_own), err_name(e_gone),
           err_name(e_other));
    fflush(stdout);
    _exit(0);
  }
  while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
  }
  close(master);
  return WIFEXITED(status) ? WEXITSTATUS(status) : 3;
}

static int split_words(char *s, char **out, int max) {
  int n = 0;
  char *tok = strtok(s, " ");
  while (tok != NULL && n < max) {
    out[n++] = tok;
    tok = strtok(NULL, " ");
  }
  return n;
}

static void reset_signals(void) {
  int sig;
  sigset_t empty;
  for (sig = 1; sig < 32; sig++) {
    if (sig != SIGKILL && sig != SIGSTOP) {
      signal(sig, SIG_DFL);
    }
  }
  sigemptyset(&empty);
  sigprocmask(SIG_SETMASK, &empty, NULL);
}

struct step {
  char marker[256];
  char bytes[128];
  size_t nbytes;
  int typed; /* 1: a command line, typed at a prompt; 0: raw keys, typed at a running job */
};

static size_t parse_octals(const char *s, char *out, size_t max) {
  size_t n = 0;
  while (*s != '\0' && n < max) {
    if (s[0] == '\\' && s[1] >= '0' && s[1] <= '7') {
      int v = 0;
      int digits = 0;
      s++;
      while (digits < 3 && *s >= '0' && *s <= '7') {
        v = v * 8 + (*s - '0');
        s++;
        digits++;
      }
      out[n++] = (char)v;
    } else {
      out[n++] = *s++;
    }
  }
  return n;
}

/* Count the prompts in a chunk of terminal output; *held carries a match across two chunks. */
static int count_prompts(const char *buf, size_t n, int *held) {
  const char *want = PROMPT;
  size_t len = strlen(want);
  int found = 0;
  size_t i;
  for (i = 0; i < n; i++) {
    if (buf[i] == want[*held]) {
      *held = *held + 1;
      if ((size_t)*held == len) {
        found++;
        *held = 0;
      }
    } else {
      *held = buf[i] == want[0] ? 1 : 0;
    }
  }
  return found;
}

/* After a timeout, show the last part of what the terminal printed, with control bytes made visible. */
static void print_tail(const char *path) {
  char buf[800];
  ssize_t n;
  ssize_t i;
  off_t size;
  int fd = open(path, O_RDONLY);
  if (fd < 0) {
    return;
  }
  size = lseek(fd, 0, SEEK_END);
  lseek(fd, size > (off_t)sizeof(buf) ? size - (off_t)sizeof(buf) : 0, SEEK_SET);
  n = read(fd, buf, sizeof(buf));
  close(fd);
  printf("terminal tail: ");
  for (i = 0; i < n; i++) {
    unsigned char c = (unsigned char)buf[i];
    if (c == '\n') {
      printf("\\n");
    } else if (c < 32 || c > 126) {
      printf("\\%03o", c);
    } else {
      putchar(c);
    }
  }
  printf("\n");
}

static int cmd_pty(int argc, char **argv) {
  const char *outdir;
  struct step steps[MAX_STEPS];
  int nsteps = 0;
  int i = 3;
  int j;
  int nwords;
  char *exec_argv[MAX_ARGV];
  char wordbuf[256];
  char slave_name[256];
  char path[512];
  char buf[4096];
  int master;
  int out_fd;
  pid_t child;
  int status = 0;
  int cur = 0;
  int held = 0;
  long prompts = 0;
  long prompts_at_last_step = 0;
  long ready_since = 0;
  long phase_start;
  int timed_out = 0;

  if (argc < 5) {
    return 2;
  }
  alarm(58);
  outdir = argv[2];
  memset(steps, 0, sizeof(steps));
  while (i + 1 < argc && (strcmp(argv[i], "--type") == 0 || strcmp(argv[i], "--key") == 0)) {
    char *eq = strchr(argv[i + 1], '=');
    struct step *st = &steps[nsteps];
    if (eq == NULL || nsteps >= MAX_STEPS) {
      return 2;
    }
    st->typed = strcmp(argv[i], "--type") == 0;
    if (eq != argv[i + 1]) {
      snprintf(st->marker, sizeof(st->marker), "%s/%.*s", outdir, (int)(eq - argv[i + 1]), argv[i + 1]);
    }
    if (st->typed) {
      st->nbytes = strlen(eq + 1);
      if (st->nbytes + 1 > sizeof(st->bytes)) {
        return 2;
      }
      memcpy(st->bytes, eq + 1, st->nbytes);
      st->bytes[st->nbytes++] = '\n';
    } else {
      st->nbytes = parse_octals(eq + 1, st->bytes, sizeof(st->bytes));
    }
    nsteps++;
    i += 2;
  }
  if (i + 3 > argc || strcmp(argv[i], "--shell") != 0 || strcmp(argv[i + 2], "--") != 0) {
    return 2;
  }
  snprintf(wordbuf, sizeof(wordbuf), "%s", argv[i + 1]);
  nwords = split_words(wordbuf, exec_argv, MAX_WORDS - 1);
  if (nwords == 0) {
    return 2;
  }
  for (j = i + 3; j < argc && nwords < MAX_ARGV - 1; j++) {
    exec_argv[nwords++] = argv[j];
  }
  exec_argv[nwords] = NULL;

  master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt(master) < 0 || unlockpt(master) < 0) {
    perror("pty");
    return 3;
  }
  snprintf(slave_name, sizeof(slave_name), "%s", ptsname(master));
  snprintf(path, sizeof(path), "%s/tty.out", outdir);
  out_fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (out_fd < 0) {
    perror("open tty.out");
    return 3;
  }

  child = fork();
  if (child < 0) {
    perror("fork");
    return 3;
  }
  if (child == 0) {
    int slave;
    struct termios t;
    close(master);
    if (setsid() < 0 || chdir(outdir) < 0) {
      _exit(126);
    }
    slave = open(slave_name, O_RDWR);
    if (slave < 0 || ioctl(slave, TIOCSCTTY, 0) < 0) {
      _exit(126);
    }
    if (tcgetattr(slave, &t) == 0) {
      t.c_lflag |= ISIG | ICANON | ECHO;
      t.c_lflag &= ~(tcflag_t)TOSTOP;
      t.c_cc[VINTR] = 3;
      t.c_cc[VQUIT] = 28;
      t.c_cc[VSUSP] = 26;
      if (tcsetattr(slave, TCSANOW, &t) < 0) {
        _exit(126);
      }
    }
    dup2(slave, 0);
    dup2(slave, 1);
    dup2(slave, 2);
    if (slave > 2) {
      close(slave);
    }
    reset_signals();
    /* An interactive shell reads startup files and prints a prompt: point the files at nothing and
     * make the prompt a marker this harness can wait for. */
    setenv("LOGDIR", outdir, 1);
    setenv("HOME", outdir, 1);
    setenv("ENV", "/dev/null", 1);
    setenv("PS1", PROMPT " ", 1);
    setenv("TERM", "dumb", 1);
    unsetenv("BASH_ENV");
    unsetenv("ZDOTDIR");
    unsetenv("PROMPT_COMMAND");
    setenv("YASH_PS1", PROMPT " ", 1); /* yash prefers this to PS1 outside its POSIX mode */
    alarm(0);
    execvp(exec_argv[0], exec_argv);
    _exit(127);
  }

  phase_start = mono_ms();
  for (;;) {
    long now;
    struct pollfd pf;
    ssize_t n = 0;
    pf.fd = master;
    pf.events = POLLIN;
    pf.revents = 0;
    if (poll(&pf, 1, 50) > 0 && (pf.revents & POLLIN)) {
      n = read(master, buf, sizeof(buf));
      if (n > 0) {
        prompts += count_prompts(buf, (size_t)n, &held);
        if (write(out_fd, buf, (size_t)n) < 0) {
          perror("write tty.out");
        }
      }
    }
    if (n <= 0) {
      sleep_ms(20);
    }
    if (waitpid(child, &status, WNOHANG) == child) {
      break;
    }
    now = mono_ms();
    if (cur < nsteps) {
      struct step *st = &steps[cur];
      int ready = st->marker[0] == '\0' || file_exists(st->marker);
      if (ready && st->typed && prompts <= prompts_at_last_step) {
        ready = 0; /* the shell has not come back to read a command yet */
      }
      if (!ready) {
        ready_since = 0;
        if (now > phase_start + 15000) {
          printf("step_timeout step=%d marker=%s prompts=%ld\n", cur + 1, st->marker, prompts);
          timed_out = 1;
          break;
        }
        continue;
      }
      if (ready_since == 0) {
        ready_since = now;
      }
      /* Margin between "ready" and the keystroke: half a second at a prompt, a second for a key
       * aimed at a running job, which has no prompt to wait for. */
      if (now >= ready_since + (st->typed ? 500 : 1000)) {
        if (write(master, st->bytes, st->nbytes) < 0) {
          perror("write master");
        }
        prompts_at_last_step = prompts;
        ready_since = 0;
        cur++;
        phase_start = now;
      }
      continue;
    }
    if (now > phase_start + 20000) {
      printf("timeout waiting for the shell to exit\n");
      timed_out = 1;
      break;
    }
  }
  if (timed_out) {
    kill(-child, SIGKILL);
    kill(child, SIGKILL);
    waitpid(child, &status, 0);
    snprintf(path, sizeof(path), "%s/tty.out", outdir);
    print_tail(path);
    return 3;
  }
  if (WIFEXITED(status)) {
    printf("child_exit=%d\n", WEXITSTATUS(status));
  } else if (WIFSIGNALED(status)) {
    printf("child_signal=%d\n", WTERMSIG(status));
  }
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    return 2;
  }
  if (strcmp(argv[1], "pty") == 0) {
    return cmd_pty(argc, argv);
  }
  if (strcmp(argv[1], "job") == 0) {
    return cmd_job(argc, argv);
  }
  if (strcmp(argv[1], "hold") == 0) {
    return cmd_hold(argc, argv);
  }
  if (strcmp(argv[1], "who") == 0) {
    return cmd_who(argc, argv);
  }
  if (strcmp(argv[1], "ttymode") == 0) {
    return cmd_ttymode(argc, argv);
  }
  if (strcmp(argv[1], "echoflag") == 0) {
    return cmd_echoflag();
  }
  if (strcmp(argv[1], "ttyerr") == 0) {
    return cmd_ttyerr();
  }
  if (strcmp(argv[1], "state") == 0) {
    return cmd_state(argc, argv);
  }
  if (strcmp(argv[1], "tstp") == 0) {
    printf("%d\n", SIGTSTP);
    return 0;
  }
  if (strcmp(argv[1], "pgid") == 0 && argc == 3) {
    printf("%ld\n", (long)getpgid((pid_t)strtol(argv[2], NULL, 10)));
    return 0;
  }
  if (strcmp(argv[1], "msleep") == 0 && argc == 3) {
    sleep_ms(strtol(argv[2], NULL, 10));
    return 0;
  }
  fprintf(stderr, "fgpg: unknown mode\n");
  return 2;
}
