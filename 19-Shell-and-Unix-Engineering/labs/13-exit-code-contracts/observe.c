/* observe.c: report how a child process ended, as its parent sees it.
 *
 *   observe run CMD [ARG...]  fork, exec CMD with stderr on /dev/null, wait,
 *                             then print "exited N" or "signaled N"
 *   observe waitid N          two children call _exit(N); print the status
 *                             waitpid() reports and the si_status waitid()
 *                             reports, as "wait=A waitid=B"
 *   observe sysexits          print the <sysexits.h> and <stdlib.h> values
 *
 * A shell's $? cannot tell "exited 143" from "killed by SIGTERM"; this can.
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sysexits.h>
#include <unistd.h>

static int run(char **argv) {
  pid_t pid = fork();
  if (pid < 0) {
    perror("fork");
    return 1;
  }
  if (pid == 0) {
    int fd = open("/dev/null", O_WRONLY);
    if (fd >= 0 && dup2(fd, 2) < 0) {
      _exit(125);
    }
    execvp(argv[0], argv);
    _exit(errno == ENOENT ? 127 : 126);
  }
  int st = 0;
  while (waitpid(pid, &st, 0) < 0) {
    if (errno != EINTR) {
      perror("waitpid");
      return 1;
    }
  }
  if (WIFEXITED(st)) {
    printf("exited %d\n", WEXITSTATUS(st));
  } else if (WIFSIGNALED(st)) {
    printf("signaled %d\n", WTERMSIG(st));
  } else {
    printf("other 0x%x\n", (unsigned)st);
  }
  return 0;
}

static int full_status(int code) {
  pid_t a = fork();
  if (a < 0) {
    perror("fork");
    return 1;
  }
  if (a == 0) {
    _exit(code);
  }
  int st = 0;
  if (waitpid(a, &st, 0) < 0 || !WIFEXITED(st)) {
    perror("waitpid");
    return 1;
  }
  pid_t b = fork();
  if (b < 0) {
    perror("fork");
    return 1;
  }
  if (b == 0) {
    _exit(code);
  }
  siginfo_t si;
  memset(&si, 0, sizeof si);
  if (waitid(P_PID, (id_t)b, &si, WEXITED) < 0 || si.si_code != CLD_EXITED) {
    perror("waitid");
    return 1;
  }
  printf("wait=%d waitid=%d\n", WEXITSTATUS(st), si.si_status);
  return 0;
}

static void sysexits(void) {
  printf("EXIT_SUCCESS=%d EXIT_FAILURE=%d\n", EXIT_SUCCESS, EXIT_FAILURE);
  printf("EX_OK=%d EX__BASE=%d EX__MAX=%d\n", EX_OK, EX__BASE, EX__MAX);
  printf("EX_USAGE=%d EX_DATAERR=%d EX_NOINPUT=%d EX_NOUSER=%d EX_NOHOST=%d\n",
         EX_USAGE, EX_DATAERR, EX_NOINPUT, EX_NOUSER, EX_NOHOST);
  printf("EX_UNAVAILABLE=%d EX_SOFTWARE=%d EX_OSERR=%d EX_OSFILE=%d\n",
         EX_UNAVAILABLE, EX_SOFTWARE, EX_OSERR, EX_OSFILE);
  printf("EX_CANTCREAT=%d EX_IOERR=%d EX_TEMPFAIL=%d EX_PROTOCOL=%d\n",
         EX_CANTCREAT, EX_IOERR, EX_TEMPFAIL, EX_PROTOCOL);
  printf("EX_NOPERM=%d EX_CONFIG=%d\n", EX_NOPERM, EX_CONFIG);
}

int main(int argc, char **argv) {
  if (argc >= 3 && strcmp(argv[1], "run") == 0) {
    return run(argv + 2);
  }
  if (argc == 3 && strcmp(argv[1], "waitid") == 0) {
    return full_status(atoi(argv[2]));
  }
  if (argc == 2 && strcmp(argv[1], "sysexits") == 0) {
    sysexits();
    return 0;
  }
  fprintf(stderr, "usage: observe run CMD [ARG...] | waitid N | sysexits\n");
  return 2;
}
