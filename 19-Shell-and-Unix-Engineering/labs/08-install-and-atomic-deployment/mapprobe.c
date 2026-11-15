/* mapprobe FILE CMD [ARG...]: map FILE read-only with MAP_SHARED, run CMD to completion,
 * then print what the mapping shows at its first and at its last byte, as
 * "first=X last=Y". If the file was truncated under the mapping, touching the last byte
 * raises SIGBUS (or SIGSEGV), and the report says so instead of a byte. */
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static void report_signal(int sig) {
  static const char bus[] = "last=SIGBUS\n";
  static const char segv[] = "last=SIGSEGV\n";
  ssize_t n;
  if (sig == SIGBUS) {
    n = write(STDOUT_FILENO, bus, sizeof bus - 1);
  } else {
    n = write(STDOUT_FILENO, segv, sizeof segv - 1);
  }
  _exit(n < 0 ? 4 : 0);
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: mapprobe FILE CMD [ARG...]\n");
    return 2;
  }
  int fd = open(argv[1], O_RDONLY);
  if (fd < 0) {
    perror(argv[1]);
    return 2;
  }
  struct stat st;
  if (fstat(fd, &st) < 0 || st.st_size < 2) {
    fprintf(stderr, "mapprobe: %s: need a file of at least 2 bytes\n", argv[1]);
    return 2;
  }
  size_t len = (size_t)st.st_size;
  const volatile char *map = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, 0);
  if (map == MAP_FAILED) {
    perror("mmap");
    return 2;
  }
  if (close(fd) < 0) {
    perror("close");
    return 2;
  }

  pid_t pid = fork();
  if (pid < 0) {
    perror("fork");
    return 2;
  }
  if (pid == 0) {
    execvp(argv[2], argv + 2);
    perror(argv[2]);
    _exit(127);
  }
  int status;
  if (waitpid(pid, &status, 0) < 0) {
    perror("waitpid");
    return 2;
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    fprintf(stderr, "mapprobe: %s failed\n", argv[2]);
    return 3;
  }

  struct sigaction sa;
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = report_signal;
  sigemptyset(&sa.sa_mask);
  if (sigaction(SIGBUS, &sa, NULL) < 0 || sigaction(SIGSEGV, &sa, NULL) < 0) {
    perror("sigaction");
    return 2;
  }
  printf("first=%c ", map[0]);
  fflush(stdout);
  printf("last=%c\n", map[len - 1]);
  return 0;
}
