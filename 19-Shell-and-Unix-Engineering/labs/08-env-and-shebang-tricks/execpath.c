/*
 * execpath: call execve(2) directly, with no PATH search and no shell
 * fallback, so the kernel's own answer is visible.
 * Usage: execpath PATH ARGV0 [ARG...]
 *   PATH is passed to execve unchanged (it may be relative, even "-x");
 *   ARGV0 becomes argv[0], so a test can see whether the kernel keeps it.
 * On failure it prints "execve: NAME" (ENOENT, ENOEXEC, EACCES, ELOOP,
 * ENAMETOOLONG, E2BIG, or errno=N) to stdout and exits 127 for ENOENT,
 * 126 otherwise, the statuses POSIX gives env(1).
 * Build: "${CC:-cc}" -o execpath execpath.c
 */
#include <errno.h>
#include <stdio.h>
#include <unistd.h>

extern char **environ;

static const char *errno_name(int e) {
  switch (e) {
  case ENOENT:
    return "ENOENT";
  case ENOEXEC:
    return "ENOEXEC";
  case EACCES:
    return "EACCES";
  case ELOOP:
    return "ELOOP";
  case ENAMETOOLONG:
    return "ENAMETOOLONG";
  case E2BIG:
    return "E2BIG";
  default:
    return NULL;
  }
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fputs("usage: execpath PATH ARGV0 [ARG...]\n", stderr);
    return 2;
  }
  execve(argv[1], &argv[2], environ);
  int e = errno;
  const char *name = errno_name(e);
  if (name != NULL) {
    printf("execve: %s\n", name);
  } else {
    printf("execve: errno=%d\n", e);
  }
  return e == ENOENT ? 127 : 126;
}
