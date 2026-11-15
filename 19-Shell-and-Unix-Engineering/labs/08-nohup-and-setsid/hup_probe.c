/*
 * hup_probe: report what nohup and setsid change in a process, and drive a
 * pseudo-terminal so the lab can show terminal-only behavior (nohup.out,
 * controlling terminals, hangups) in a suite that has no terminal.
 *
 * Modes:
 *   hup                      print SIGHUP=ignored|default|caught
 *   session                  print leader=0|1 ctty=yes|no
 *   stdio                    print fd0=... fd1=... fd2=...
 *   ids                      print pid=N ppid=N pgid=N sid=N
 *   leader CMD...            become a process group leader, then exec CMD
 *   newsession CMD...        what setsid(1) does: fork only if leading a
 *                            process group, call setsid(), exec CMD
 *   pty SECONDS CMD...       run CMD as a session leader on a new pty, copy
 *                            its output (CR removed), then print exit=N,
 *                            signal=N, or timeout
 *   pty-hangup MARK SECONDS CMD...
 *                            like pty, but close the master side as soon as
 *                            MARK appears in the output (a terminal hangup),
 *                            then print leader=gone or leader=alive
 */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
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
#include <time.h>
#include <unistd.h>

static int mode_hup(void) {
  struct sigaction sa;
  if (sigaction(SIGHUP, NULL, &sa) < 0) {
    perror("sigaction");
    return 1;
  }
  const char *what = "caught";
  if (sa.sa_handler == SIG_IGN) {
    what = "ignored";
  } else if (sa.sa_handler == SIG_DFL) {
    what = "default";
  }
  printf("SIGHUP=%s\n", what);
  return 0;
}

static int mode_session(void) {
  int leader = getsid(0) == getpid();
  int fd = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (fd >= 0) {
    close(fd);
    printf("leader=%d ctty=yes\n", leader);
  } else if (errno == ENXIO) {
    printf("leader=%d ctty=no\n", leader);
  } else {
    printf("leader=%d ctty=error-%d\n", leader, errno);
  }
  return 0;
}

static const char *kind_in(int fd) {
  struct stat st;
  if (fstat(fd, &st) < 0) {
    return "closed";
  }
  if (isatty(fd)) {
    return "tty";
  }
  int flags = fcntl(fd, F_GETFL);
  if (flags >= 0 && (flags & O_ACCMODE) == O_WRONLY) {
    return "unreadable";
  }
  return "readable";
}

static const char *kind_out(int fd) {
  struct stat st;
  if (fstat(fd, &st) < 0) {
    return "closed";
  }
  if (isatty(fd)) {
    return "tty";
  }
  return S_ISREG(st.st_mode) ? "regular" : "other";
}

static const char *kind_err(void) {
  struct stat out;
  struct stat err;
  if (fstat(2, &err) < 0) {
    return "closed";
  }
  if (isatty(2)) {
    return "tty";
  }
  if (fstat(1, &out) == 0 && S_ISREG(out.st_mode) && S_ISREG(err.st_mode) &&
      out.st_dev == err.st_dev && out.st_ino == err.st_ino) {
    return "same-as-fd1";
  }
  return S_ISREG(err.st_mode) ? "regular" : "other";
}

static int mode_stdio(void) {
  /* Compute all three before printing: printing can change nothing here,
   * but keep the observation separate from the report. */
  const char *k0 = kind_in(0);
  const char *k1 = kind_out(1);
  const char *k2 = kind_err();
  printf("fd0=%s fd1=%s fd2=%s\n", k0, k1, k2);
  return 0;
}

static int mode_ids(void) {
  printf("pid=%ld ppid=%ld pgid=%ld sid=%ld\n", (long)getpid(),
         (long)getppid(), (long)getpgrp(), (long)getsid(0));
  return 0;
}

static int mode_leader(char **cmd) {
  if (setpgid(0, 0) < 0) {
    perror("setpgid");
    return 1;
  }
  execvp(cmd[0], cmd);
  perror(cmd[0]);
  return 127;
}

static int mode_newsession(char **cmd) {
  if (getpgrp() == getpid()) {
    pid_t pid = fork();
    if (pid < 0) {
      perror("fork");
      return 1;
    }
    if (pid > 0) {
      _exit(0);
    }
  }
  if (setsid() < 0) {
    perror("setsid");
    return 1;
  }
  execvp(cmd[0], cmd);
  perror(cmd[0]);
  return 127;
}

static double now_seconds(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* Copy buf to stdout without carriage returns; keep a tail for marker search. */
static void emit(const char *buf, ssize_t n, char *seen, size_t cap) {
  for (ssize_t i = 0; i < n; i++) {
    if (buf[i] == '\r') {
      continue;
    }
    if (fputc(buf[i], stdout) == EOF) {
      return;
    }
    size_t len = strlen(seen);
    if (len + 1 >= cap) {
      memmove(seen, seen + cap / 2, len - cap / 2 + 1);
      len = strlen(seen);
    }
    seen[len] = buf[i];
    seen[len + 1] = '\0';
  }
  fflush(stdout);
}

static int run_on_pty(const char *mark, double seconds, char **cmd) {
  int master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt(master) < 0 || unlockpt(master) < 0) {
    perror("posix_openpt");
    return 1;
  }
  const char *name = ptsname(master);
  if (name == NULL) {
    perror("ptsname");
    return 1;
  }
  char slave_name[256];
  snprintf(slave_name, sizeof slave_name, "%s", name);

  fflush(stdout);
  pid_t child = fork();
  if (child < 0) {
    perror("fork");
    return 1;
  }
  if (child == 0) {
    /* A new session, then make the pty slave its controlling terminal. */
    if (setsid() < 0) {
      perror("setsid");
      _exit(126);
    }
    int slave = open(slave_name, O_RDWR | O_NOCTTY);
    if (slave < 0) {
      perror("open slave");
      _exit(126);
    }
    if (ioctl(slave, TIOCSCTTY, 0) < 0) {
      perror("TIOCSCTTY");
      _exit(126);
    }
    close(master);
    if (dup2(slave, 0) < 0 || dup2(slave, 1) < 0 || dup2(slave, 2) < 0) {
      _exit(126);
    }
    if (slave > 2) {
      close(slave);
    }
    execvp(cmd[0], cmd);
    perror(cmd[0]);
    _exit(127);
  }

  double deadline = now_seconds() + seconds;
  char seen[4096] = "";
  char buf[1024];
  int hung_up = 0;
  while (master >= 0) {
    int left = (int)((deadline - now_seconds()) * 1000);
    if (left <= 0) {
      break;
    }
    struct pollfd p = {master, POLLIN, 0};
    int r = poll(&p, 1, left);
    if (r < 0 && errno == EINTR) {
      continue;
    }
    if (r <= 0) {
      break;
    }
    ssize_t n = read(master, buf, sizeof buf);
    if (n <= 0) {
      /* 0 or EIO: every slave descriptor is closed. */
      break;
    }
    emit(buf, n, seen, sizeof seen);
    /* Hang up only once the whole marker line has arrived. */
    const char *hit = mark != NULL ? strstr(seen, mark) : NULL;
    if (hit != NULL && hit[strlen(mark)] == '\n') {
      close(master);
      master = -1;
      hung_up = 1;
      printf("hangup-sent\n");
    }
  }

  int status = 0;
  pid_t got = 0;
  while ((got = waitpid(child, &status, WNOHANG)) == 0 &&
         now_seconds() < deadline) {
    struct timespec pause = {0, 50 * 1000 * 1000};
    nanosleep(&pause, NULL);
  }
  if (master >= 0) {
    close(master);
  }
  if (got == 0) {
    /* Leave nothing behind: kill the whole session's leader group. */
    kill(-child, SIGKILL);
    kill(child, SIGKILL);
    waitpid(child, &status, 0);
    printf(mark != NULL ? "leader=alive\n" : "timeout\n");
    return 1;
  }
  if (mark != NULL) {
    printf(hung_up ? "leader=gone\n" : "no-mark\n");
    return hung_up ? 0 : 1;
  }
  if (WIFEXITED(status)) {
    printf("exit=%d\n", WEXITSTATUS(status));
  } else if (WIFSIGNALED(status)) {
    printf("signal=%d\n", WTERMSIG(status));
  }
  return 0;
}

static int usage(void) {
  fprintf(stderr, "usage: hup_probe hup|session|stdio|ids|leader CMD|"
                  "newsession CMD|pty SECONDS CMD|"
                  "pty-hangup MARK SECONDS CMD\n");
  return 2;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    return usage();
  }
  const char *mode = argv[1];
  if (strcmp(mode, "hup") == 0) {
    return mode_hup();
  }
  if (strcmp(mode, "session") == 0) {
    return mode_session();
  }
  if (strcmp(mode, "stdio") == 0) {
    return mode_stdio();
  }
  if (strcmp(mode, "ids") == 0) {
    return mode_ids();
  }
  if (strcmp(mode, "leader") == 0 && argc >= 3) {
    return mode_leader(argv + 2);
  }
  if (strcmp(mode, "newsession") == 0 && argc >= 3) {
    return mode_newsession(argv + 2);
  }
  if (strcmp(mode, "pty") == 0 && argc >= 4) {
    return run_on_pty(NULL, atof(argv[2]), argv + 3);
  }
  if (strcmp(mode, "pty-hangup") == 0 && argc >= 5) {
    return run_on_pty(argv[2], atof(argv[3]), argv + 4);
  }
  return usage();
}
