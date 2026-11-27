/*
 * oiprobe: probes for append atomicity and output interleaving.
 *
 *   appendw FILE COUNT SIZE CHAR   open(O_APPEND), then COUNT single write(2) calls of SIZE bytes of CHAR
 *   blocks FILE SIZE               check FILE in SIZE-byte blocks; report how many are one repeated byte
 *   race append|plain FILE         two processes, a fixed schedule: B appends while A sits between
 *                                  "find the end" and "write"; report what the file holds
 *   pwrite-append FILE             O_APPEND descriptor, pwrite("XY", offset 0) into "0123456789"
 *   stdio-writes LEN BUFSZ         count the write(2) calls stdio makes for one fwrite of LEN bytes
 *                                  through a BUFSZ-byte full buffer
 *   count-writes CMD [ARG...]      run CMD with stdout on a datagram socket; every write(2) it makes
 *                                  arrives as one datagram, so the datagrams are the writes
 *   order [unbuf]                  stdout "A", stderr "B", stdout "C"; stdout is fully buffered
 *                                  (or unbuffered with "unbuf")
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum { MAX_DGRAMS = 4096, DGRAM_MAX = 1 << 16, DEADLINE_SECONDS = 20 };

static int usage(void) {
  fputs("usage: oiprobe appendw|blocks|race|pwrite-append|stdio-writes|count-writes|order ...\n", stderr);
  return 2;
}

static int fail(const char *what) {
  fprintf(stderr, "oiprobe: %s: %s\n", what, strerror(errno));
  return 2;
}

static int cmd_appendw(int argc, char **argv) {
  if (argc != 6) {
    return usage();
  }
  long count = atol(argv[3]);
  long size = atol(argv[4]);
  if (count <= 0 || size <= 0 || argv[5][0] == '\0') {
    return usage();
  }
  int fd = open(argv[2], O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) {
    return fail("open");
  }
  char *buf = malloc((size_t)size);
  if (buf == NULL) {
    return fail("malloc");
  }
  memset(buf, argv[5][0], (size_t)size);
  for (long i = 0; i < count; i++) {
    ssize_t n = write(fd, buf, (size_t)size);
    if (n != (ssize_t)size) {
      fprintf(stderr, "oiprobe: short or failed write %zd (%s)\n", n, strerror(errno));
      return 1;
    }
  }
  free(buf);
  return close(fd) == 0 ? 0 : fail("close");
}

static int cmd_blocks(int argc, char **argv) {
  if (argc != 4) {
    return usage();
  }
  long size = atol(argv[3]);
  if (size <= 0) {
    return usage();
  }
  FILE *fp = fopen(argv[2], "rb");
  if (fp == NULL) {
    return fail("fopen");
  }
  unsigned char *buf = malloc((size_t)size);
  if (buf == NULL) {
    return fail("malloc");
  }
  long blocks = 0, uniform = 0, mixed = 0, partial = 0;
  long per_byte[256] = {0};
  size_t n;
  while ((n = fread(buf, 1, (size_t)size, fp)) > 0) {
    if (n < (size_t)size) {
      partial = 1;
      break;
    }
    blocks++;
    size_t i = 1;
    while (i < n && buf[i] == buf[0]) {
      i++;
    }
    if (i == n) {
      uniform++;
      per_byte[buf[0]]++;
    } else {
      mixed++;
    }
  }
  fclose(fp);
  free(buf);
  printf("blocks=%ld uniform=%ld mixed=%ld partial=%ld", blocks, uniform, mixed, partial);
  for (int c = 0; c < 256; c++) {
    if (per_byte[c] > 0) {
      printf(" %c=%ld", c, per_byte[c]);
    }
  }
  printf("\n");
  return 0;
}

/* Read one byte from fd; 0 on success. */
static int get_byte(int fd) {
  char c;
  return read(fd, &c, 1) == 1 ? 0 : -1;
}

static int put_byte(int fd) {
  return write(fd, "x", 1) == 1 ? 0 : -1;
}

static int cmd_race(int argc, char **argv) {
  if (argc != 4) {
    return usage();
  }
  int append = strcmp(argv[2], "append") == 0;
  if (!append && strcmp(argv[2], "plain") != 0) {
    return usage();
  }
  const char *path = argv[3];
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (fd < 0) {
    return fail("create");
  }
  close(fd);
  int ready[2], go[2];
  if (pipe(ready) != 0 || pipe(go) != 0) {
    return fail("pipe");
  }
  pid_t pid = fork();
  if (pid < 0) {
    return fail("fork");
  }
  if (pid == 0) {
    /* Process A: find the end of the file, stall, then write. */
    int a = open(path, O_WRONLY | (append ? O_APPEND : 0));
    if (a < 0) {
      _exit(3);
    }
    if (!append && lseek(a, 0, SEEK_END) < 0) {
      _exit(3);
    }
    if (put_byte(ready[1]) != 0 || get_byte(go[0]) != 0) {
      _exit(3);
    }
    _exit(write(a, "AAAA", 4) == 4 ? 0 : 4);
  }
  /* Process B: wait until A has chosen its position, append "BBBB", release A. */
  if (get_byte(ready[0]) != 0) {
    return fail("ready");
  }
  int b = open(path, O_WRONLY | O_APPEND);
  if (b < 0 || write(b, "BBBB", 4) != 4) {
    return fail("append B");
  }
  close(b);
  if (put_byte(go[1]) != 0) {
    return fail("go");
  }
  int st = 0;
  if (waitpid(pid, &st, 0) != pid || !WIFEXITED(st) || WEXITSTATUS(st) != 0) {
    fprintf(stderr, "oiprobe: writer A failed (status %d)\n", st);
    return 1;
  }
  char content[64];
  FILE *fp = fopen(path, "rb");
  if (fp == NULL) {
    return fail("reopen");
  }
  size_t n = fread(content, 1, sizeof content - 1, fp);
  fclose(fp);
  content[n] = '\0';
  printf("size=%zu content=%s\n", n, content);
  return 0;
}

static int cmd_pwrite_append(int argc, char **argv) {
  if (argc != 3) {
    return usage();
  }
  int fd = open(argv[2], O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (fd < 0 || write(fd, "0123456789", 10) != 10) {
    return fail("seed");
  }
  close(fd);
  fd = open(argv[2], O_WRONLY | O_APPEND);
  if (fd < 0) {
    return fail("open append");
  }
  if (pwrite(fd, "XY", 2, 0) != 2) {
    return fail("pwrite");
  }
  close(fd);
  char content[64];
  FILE *fp = fopen(argv[2], "rb");
  if (fp == NULL) {
    return fail("reopen");
  }
  size_t n = fread(content, 1, sizeof content - 1, fp);
  fclose(fp);
  content[n] = '\0';
  printf("content=%s\n", content);
  return 0;
}

/*
 * Run ACTION in a child whose stdout is one end of a datagram socketpair and collect the datagrams
 * in the parent: one datagram per write(2) the child makes. Returns the child's wait status or -1.
 */
struct counted {
  long writes;
  long bytes;
  long sizes[MAX_DGRAMS];
};

static int run_counted(struct counted *out, void (*action)(void *), void *arg) {
  int sv[2];
  if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sv) != 0) {
    return -1;
  }
  pid_t pid = fork();
  if (pid < 0) {
    return -1;
  }
  if (pid == 0) {
    close(sv[0]);
    if (dup2(sv[1], STDOUT_FILENO) < 0) {
      _exit(120);
    }
    close(sv[1]);
    action(arg);
    _exit(121);
  }
  close(sv[1]);
  static char buf[DGRAM_MAX];
  time_t deadline = time(NULL) + DEADLINE_SECONDS;
  int status = 0;
  int done = 0;
  out->writes = 0;
  out->bytes = 0;
  for (;;) {
    if (!done && waitpid(pid, &status, WNOHANG) == pid) {
      done = 1;
    }
    struct pollfd p = {.fd = sv[0], .events = POLLIN, .revents = 0};
    int r = poll(&p, 1, 50);
    if (r > 0 && (p.revents & POLLIN)) {
      ssize_t n = recv(sv[0], buf, sizeof buf, 0);
      if (n >= 0) {
        if (out->writes < MAX_DGRAMS) {
          out->sizes[out->writes] = (long)n;
        }
        out->writes++;
        out->bytes += (long)n;
      }
    } else if (done) {
      break;
    }
    if (time(NULL) > deadline) {
      kill(pid, SIGKILL);
      waitpid(pid, &status, 0);
      close(sv[0]);
      return -1;
    }
  }
  close(sv[0]);
  return status;
}

static void print_counted(const struct counted *c, int status) {
  printf("writes=%ld bytes=%ld status=%d sizes=", c->writes, c->bytes,
         WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status));
  for (long i = 0; i < c->writes && i < MAX_DGRAMS; i++) {
    printf("%s%ld", i ? "," : "", c->sizes[i]);
  }
  printf("\n");
}

struct stdio_job {
  long len;
  long bufsz;
};

static void stdio_action(void *arg) {
  struct stdio_job *job = arg;
  char *buf = malloc((size_t)job->len);
  if (buf == NULL || setvbuf(stdout, NULL, _IOFBF, (size_t)job->bufsz) != 0) {
    _exit(122);
  }
  memset(buf, 'x', (size_t)job->len);
  if (fwrite(buf, 1, (size_t)job->len, stdout) != (size_t)job->len) {
    _exit(123);
  }
  exit(0); /* flushes stdout */
}

static int cmd_stdio_writes(int argc, char **argv) {
  if (argc != 4) {
    return usage();
  }
  struct stdio_job job = {atol(argv[2]), atol(argv[3])};
  if (job.len <= 0 || job.bufsz <= 0) {
    return usage();
  }
  static struct counted c;
  int st = run_counted(&c, stdio_action, &job);
  if (st < 0) {
    return fail("run_counted");
  }
  print_counted(&c, st);
  return 0;
}

static void exec_action(void *arg) {
  char **argv = arg;
  execvp(argv[0], argv);
  _exit(127);
}

static int cmd_count_writes(int argc, char **argv) {
  if (argc < 3) {
    return usage();
  }
  static struct counted c;
  int st = run_counted(&c, exec_action, argv + 2);
  if (st < 0) {
    return fail("run_counted");
  }
  print_counted(&c, st);
  return 0;
}

static int cmd_order(int argc, char **argv) {
  if (argc == 3 && strcmp(argv[2], "unbuf") == 0) {
    if (setvbuf(stdout, NULL, _IONBF, 0) != 0) {
      return fail("setvbuf");
    }
  } else if (argc != 2) {
    return usage();
  }
  fputs("A\n", stdout);
  if (write(STDERR_FILENO, "B\n", 2) != 2) {
    return fail("write stderr");
  }
  fputs("C\n", stdout);
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    return usage();
  }
  const char *m = argv[1];
  if (strcmp(m, "appendw") == 0) {
    return cmd_appendw(argc, argv);
  }
  if (strcmp(m, "blocks") == 0) {
    return cmd_blocks(argc, argv);
  }
  if (strcmp(m, "race") == 0) {
    return cmd_race(argc, argv);
  }
  if (strcmp(m, "pwrite-append") == 0) {
    return cmd_pwrite_append(argc, argv);
  }
  if (strcmp(m, "stdio-writes") == 0) {
    return cmd_stdio_writes(argc, argv);
  }
  if (strcmp(m, "count-writes") == 0) {
    return cmd_count_writes(argc, argv);
  }
  if (strcmp(m, "order") == 0) {
    return cmd_order(argc, argv);
  }
  return usage();
}
