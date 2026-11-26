/*
 * writes.c: count the write(2) calls a command makes on one descriptor.
 *
 * Usage: writes FD CMD [ARG...]
 *
 * Connects descriptor FD of CMD to one end of an AF_UNIX SOCK_DGRAM socket pair.
 * A datagram socket keeps message boundaries, so every write(2) the command makes on FD
 * arrives as exactly one datagram; a pipe or a file would merge them.
 * Prints "writes=N bytes=M" and then each datagram on its own line as "[TEXT]",
 * with a trailing newline in TEXT shown as "\n".
 * Gives up after 20 seconds, kills the command, and exits 3.
 */
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAXMSG 64
#define MSGSIZE 4096

static char msgs[MAXMSG][MSGSIZE + 1];
static ssize_t lens[MAXMSG];

int main(int argc, char **argv) {
  int sv[2];
  int fd;
  int status = 0;
  int exited = 0;
  int n = 0;
  int ticks = 0;
  long total = 0;
  pid_t pid;

  if (argc < 3) {
    fprintf(stderr, "usage: writes FD CMD [ARG...]\n");
    return 2;
  }
  fd = atoi(argv[1]);
  if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sv) < 0) {
    perror("socketpair");
    return 2;
  }
  pid = fork();
  if (pid < 0) {
    perror("fork");
    return 2;
  }
  if (pid == 0) {
    close(sv[0]);
    if (dup2(sv[1], fd) < 0) {
      perror("dup2");
      _exit(126);
    }
    if (sv[1] != fd) {
      close(sv[1]);
    }
    execvp(argv[2], argv + 2);
    perror("execvp");
    _exit(127);
  }
  close(sv[1]);
  for (;;) {
    struct pollfd p;
    int r;

    p.fd = sv[0];
    p.events = POLLIN;
    p.revents = 0;
    r = poll(&p, 1, 100);
    if (r > 0 && (p.revents & POLLIN)) {
      char buf[MSGSIZE + 1];
      ssize_t got = recv(sv[0], buf, MSGSIZE, 0);
      if (got < 0) {
        if (errno == EINTR) {
          continue;
        }
        if (errno == ECONNRESET || errno == ECONNREFUSED) {
          /* macOS reports the peer's close this way once the queue is empty */
          if (!exited) {
            if (waitpid(pid, &status, 0) < 0) {
              perror("waitpid");
              return 2;
            }
            exited = 1;
          }
          break;
        }
        perror("recv");
        return 2;
      }
      if (n < MAXMSG) {
        memcpy(msgs[n], buf, (size_t)got);
        msgs[n][got] = '\0';
        lens[n] = got;
      }
      n++;
      total += got;
      continue;
    }
    if (exited) {
      break; /* the command has exited and the socket stayed quiet for 100 ms */
    }
    if (waitpid(pid, &status, WNOHANG) == pid) {
      exited = 1;
      continue; /* drain what is still queued */
    }
    if (++ticks > 200) {
      kill(pid, SIGKILL);
      if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
      }
      fprintf(stderr, "writes: timed out\n");
      return 3;
    }
  }
  printf("writes=%d bytes=%ld\n", n, total);
  for (int i = 0; i < n && i < MAXMSG; i++) {
    ssize_t len = lens[i];
    int nl = len > 0 && msgs[i][len - 1] == '\n';
    if (nl) {
      msgs[i][len - 1] = '\0';
    }
    printf("[%s%s]\n", msgs[i], nl ? "\\n" : "");
  }
  return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
