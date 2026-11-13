/* launch: run a program with a chosen argv[0], or with standard input one end of a connected
 * socket pair (the way sshd starts a remote command), and exit with the program's status.
 * Usage: launch [-a NAME] [-s] PATH [ARG...]
 *   -a NAME  pass NAME as argv[0] instead of PATH (login(1) passes "-bash" to mark a login shell)
 *   -s       make descriptor 0 a connected AF_UNIX stream socket; the parent keeps the peer open
 *            until the program exits, so getpeername(2) on descriptor 0 succeeds, which is the
 *            test bash's isnetconn() applies; the program reads end-of-file if it reads input
 * PATH is run with execv, so it must name the file (no PATH search). */
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int usage(void) {
  fprintf(stderr, "usage: launch [-a NAME] [-s] PATH [ARG...]\n");
  return 2;
}

int main(int argc, char **argv) {
  const char *name = NULL;
  const char *file;
  char **args;
  int use_socket = 0;
  int i = 1;
  int sv[2];
  int status;
  pid_t pid;

  while (i < argc && argv[i][0] == '-') {
    if (strcmp(argv[i], "-a") == 0 && i + 1 < argc) {
      name = argv[i + 1];
      i += 2;
    } else if (strcmp(argv[i], "-s") == 0) {
      use_socket = 1;
      i += 1;
    } else {
      return usage();
    }
  }
  if (i >= argc) {
    return usage();
  }
  file = argv[i];
  if (name != NULL) {
    argv[i] = (char *)name; /* what the program sees as argv[0] */
  }
  args = argv + i;

  if (!use_socket) {
    execv(file, args);
    perror("launch: execv");
    return 127;
  }

  if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0) {
    perror("launch: socketpair");
    return 2;
  }
  pid = fork();
  if (pid < 0) {
    perror("launch: fork");
    return 2;
  }
  if (pid == 0) {
    if (dup2(sv[0], 0) < 0) {
      perror("launch: dup2");
      _exit(2);
    }
    close(sv[0]);
    close(sv[1]);
    execv(file, args);
    perror("launch: execv");
    _exit(127);
  }
  close(sv[0]);
  /* Stop sending, so a read sees end-of-file, but keep the descriptor so the peer exists. */
  if (shutdown(sv[1], SHUT_WR) < 0) {
    perror("launch: shutdown");
    return 2;
  }
  if (waitpid(pid, &status, 0) < 0) {
    perror("launch: waitpid");
    return 2;
  }
  close(sv[1]);
  if (WIFEXITED(status)) {
    return WEXITSTATUS(status);
  }
  return 128 + WTERMSIG(status);
}
