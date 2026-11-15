/* exitprog READY SECONDS: create the file READY, sleep SECONDS, and exit with CODE.
 * Compile twice with different -DCODE values to get an "old" and a "new" executable
 * for replacing a program while it runs. */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef CODE
#define CODE 7
#endif

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: exitprog READY SECONDS\n");
    return 2;
  }
  int fd = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0) {
    perror(argv[1]);
    return 2;
  }
  if (close(fd) < 0) {
    perror("close");
    return 2;
  }
  unsigned int seconds = (unsigned int)strtoul(argv[2], NULL, 10);
  while (seconds > 0) {
    seconds = sleep(seconds);
  }
  return CODE;
}
