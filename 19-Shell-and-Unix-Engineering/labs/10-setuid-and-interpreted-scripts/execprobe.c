/* execprobe.c - call execve(PATH, [PATH, ARG...], environ) exactly once.
 * Unlike a shell or execvp(), it performs no PATH search and does not prefix "./",
 * so a relative PATH such as "-i" reaches the kernel, and therefore the #!
 * interpreter, byte for byte.
 * Usage: execprobe PATH [ARG...]
 * Exit status: 127 with a diagnostic on stderr if execve() fails.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char **argv)
{
  if (argc < 2) {
    fprintf(stderr, "usage: execprobe PATH [ARG...]\n");
    return 2;
  }
  execve(argv[1], argv + 1, environ);
  fprintf(stderr, "execprobe: execve %s: %s\n", argv[1], strerror(errno));
  return 127;
}
