/* peak-rss CMD [ARG...]: run CMD with stdout sent to /dev/null, then print
 * "<peak resident set size in bytes> <exit status>" for the child.
 * ru_maxrss is in bytes on macOS and in KiB on Linux and the BSDs. */
#include <fcntl.h>
#include <stdio.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: peak-rss CMD [ARG...]\n");
    return 2;
  }
  pid_t pid = fork();
  if (pid < 0) {
    return 1;
  }
  if (pid == 0) {
    int fd = open("/dev/null", O_WRONLY);
    if (fd < 0 || dup2(fd, 1) < 0) {
      _exit(126);
    }
    execvp(argv[1], argv + 1);
    _exit(127);
  }
  int status = 0;
  if (waitpid(pid, &status, 0) != pid) {
    return 1;
  }
  struct rusage ru;
  if (getrusage(RUSAGE_CHILDREN, &ru) != 0) {
    return 1;
  }
  long long bytes = ru.ru_maxrss;
#ifndef __APPLE__
  bytes *= 1024;
#endif
  int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
  printf("%lld %d\n", bytes, code);
  return 0;
}
