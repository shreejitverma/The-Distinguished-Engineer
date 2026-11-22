/*
 * pyshim: reproduce, in C, what CPython does at the process boundary, so the
 * shell side of a Python companion can be tested on images that have no python3.
 *
 *   pyshim ignpipe CMD [ARG...]  set SIGPIPE to SIG_IGN, then execvp CMD; this is
 *                                the disposition CPython installs at startup
 *                                (Modules/signalmodule.c, signal_install_handlers)
 *                                and that os.system(), os.exec*() and
 *                                subprocess with restore_signals=False pass on
 *   pyshim sigstate              print "SIGPIPE=ignored" or "SIGPIPE=default"
 *   pyshim pipe                  fork a writer into a pipe whose reader takes one
 *                                byte and exits; report how the writer ended
 *   pyshim exit N                call exit(N), as sys.exit(N) ends in exit(N)
 *
 * Invoked through a link named "fakepy" it is a fake interpreter instead: it
 * prints its arguments, the bytes of the first non-option argument (the
 * program file), and everything on standard input, so a test can see exactly
 * what a wrapper script hands to python3.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int usage(void) {
  fputs("usage: pyshim ignpipe CMD [ARG...] | sigstate | pipe | exit N\n", stderr);
  return 2;
}

static void copy_stream(FILE *in) {
  int c;
  while ((c = getc(in)) != EOF) {
    putchar(c);
  }
}

static int fakepy(int argc, char **argv) {
  const char *program = NULL;
  FILE *f;
  int i;
  printf("argv:");
  for (i = 1; i < argc; i++) {
    printf(" <%s>", argv[i]);
    if (program == NULL && argv[i][0] != '-') {
      program = argv[i];
    }
  }
  printf("\n");
  if (program == NULL) {
    fputs("fakepy: no program file\n", stderr);
    return 2;
  }
  f = fopen(program, "r");
  if (f == NULL) {
    perror(program);
    return 2;
  }
  printf("program:\n");
  copy_stream(f);
  fclose(f);
  printf("stdin:\n");
  copy_stream(stdin);
  return fflush(stdout) == 0 ? 0 : 1;
}

static int sigstate(void) {
  struct sigaction sa;
  if (sigaction(SIGPIPE, NULL, &sa) != 0) {
    perror("sigaction");
    return 1;
  }
  printf("SIGPIPE=%s\n", sa.sa_handler == SIG_IGN ? "ignored" : "default");
  return 0;
}

/* The writer loops on write(2); with SIGPIPE at SIG_DFL the kernel kills it,
 * with SIG_IGN the write fails with EPIPE and the writer exits 3. */
static int pipe_probe(void) {
  int fds[2], status;
  pid_t writer, reader;
  char byte;
  static const char line[] = "y\n";
  if (pipe(fds) != 0) {
    perror("pipe");
    return 1;
  }
  reader = fork();
  if (reader < 0) {
    perror("fork");
    return 1;
  }
  if (reader == 0) {
    close(fds[1]);
    if (read(fds[0], &byte, 1) < 0) {
      _exit(1); /* the reader only needs to consume a byte and leave */
    }
    _exit(0);
  }
  writer = fork();
  if (writer < 0) {
    perror("fork");
    return 1;
  }
  if (writer == 0) {
    close(fds[0]);
    for (;;) {
      if (write(fds[1], line, sizeof line - 1) < 0) {
        _exit(errno == EPIPE ? 3 : 4);
      }
    }
  }
  close(fds[0]);
  close(fds[1]);
  if (waitpid(reader, &status, 0) < 0 || waitpid(writer, &status, 0) < 0) {
    perror("waitpid");
    return 1;
  }
  if (WIFSIGNALED(status)) {
    printf("writer killed by signal %d\n", WTERMSIG(status));
  } else if (WEXITSTATUS(status) == 3) {
    printf("writer got EPIPE\n");
  } else {
    printf("writer exited %d\n", WEXITSTATUS(status));
  }
  return 0;
}

int main(int argc, char **argv) {
  const char *base = strrchr(argv[0], '/');
  base = base ? base + 1 : argv[0];
  if (strcmp(base, "fakepy") == 0) {
    return fakepy(argc, argv);
  }
  if (argc >= 3 && strcmp(argv[1], "ignpipe") == 0) {
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
      perror("signal");
      return 1;
    }
    execvp(argv[2], argv + 2);
    perror(argv[2]);
    return 127;
  }
  if (argc == 2 && strcmp(argv[1], "sigstate") == 0) {
    return sigstate();
  }
  if (argc == 2 && strcmp(argv[1], "pipe") == 0) {
    return pipe_probe();
  }
  if (argc == 3 && strcmp(argv[1], "exit") == 0) {
    exit((int)strtol(argv[2], NULL, 10));
  }
  return usage();
}
