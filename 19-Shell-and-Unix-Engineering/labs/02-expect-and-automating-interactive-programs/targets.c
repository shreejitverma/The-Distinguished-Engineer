/*
 * targets: two small interactive programs that a here-document cannot drive.
 *
 * Usage:
 *   targets stdio     print a banner line and the prompt line "ready" with stdio, read one line
 *                     from stdin, print "got LINE". stdio fully buffers stdout unless it is a
 *                     terminal, so on a pipe "ready" stays in the buffer until the program exits
 *                     (musl writes the first line of a stream at once, hence the banner).
 *   targets getpass   ask for a password the way getpass(3) and readpassphrase(3) do: open
 *                     /dev/tty, switch echo off with tcsetattr(TCSAFLUSH), which discards every
 *                     byte typed so far, then print the prompt, read one line, restore the
 *                     terminal, and print the line on stdout. Exit 2 when there is no
 *                     controlling terminal.
 */
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

static int mode_stdio(void) {
  char line[256];
  printf("stdio target\n");
  printf("ready\n"); /* no fflush: that is the point */
  if (fgets(line, sizeof line, stdin) == NULL) {
    printf("got end of file\n");
    return 1;
  }
  line[strcspn(line, "\n")] = '\0';
  printf("got %s\n", line);
  return 0;
}

static int mode_getpass(void) {
  static const char prompt[] = "Password: ";
  struct termios saved, quiet;
  char line[256];
  size_t len = 0;
  int tty = open("/dev/tty", O_RDWR);
  if (tty < 0 || tcgetattr(tty, &saved) < 0) {
    return 2;
  }
  quiet = saved;
  quiet.c_lflag &= ~(tcflag_t)ECHO;
  /* echo off first, prompt second: a driver that answers the prompt can then never be echoed or flushed */
  if (tcsetattr(tty, TCSAFLUSH, &quiet) < 0) {
    return 2;
  }
  if (write(tty, prompt, sizeof prompt - 1) < 0) {
    return 2;
  }
  for (;;) {
    char c;
    ssize_t n = read(tty, &c, 1);
    if (n != 1 || c == '\n') {
      break;
    }
    if (len + 1 < sizeof line) {
      line[len++] = c;
    }
  }
  line[len] = '\0';
  if (tcsetattr(tty, TCSAFLUSH, &saved) < 0 || write(tty, "\n", 1) < 0) {
    return 2;
  }
  close(tty);
  printf("%s\n", line);
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "stdio") == 0) {
    return mode_stdio();
  }
  if (argc == 2 && strcmp(argv[1], "getpass") == 0) {
    return mode_getpass();
  }
  fprintf(stderr, "usage: targets stdio | getpass\n");
  return 2;
}
