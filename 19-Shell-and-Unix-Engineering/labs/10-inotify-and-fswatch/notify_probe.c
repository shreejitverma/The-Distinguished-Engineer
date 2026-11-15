/*
 * notify_probe.c - what the kernel's file-change notification API reports for a
 * fixed script of filesystem operations that this process performs itself.
 * Linux uses inotify(7); macOS and the BSDs use kqueue(2) with EVFILT_VNODE.
 *
 * Modes:
 *   backend         print "inotify" or "kqueue"
 *   dir DIR         watch DIR, then create+write a.txt, rename it to b.txt,
 *                   mkdir sub, unlink b.txt, rmdir sub; print what was queued
 *   coalesce FILE   5 appends left unread versus 2 appends read after each
 *   overflow DIR    create and unlink a file far more often than the queue holds
 *   replace DIR     watch DIR/target, then save it the way editors do
 *                   (write DIR/target.tmp, rename it over target), then rewrite
 *                   target in place; print what the watch saw after each step
 *
 * Every operation is a synchronous system call made before the queue is read,
 * and notification is queued inside that call, so the output is deterministic.
 */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/inotify.h>
#define BACKEND "inotify"
#elif defined(__APPLE__) || defined(__FreeBSD__) || defined(__NetBSD__) || defined(__OpenBSD__)
#include <sys/event.h>
#include <sys/time.h>
#define BACKEND "kqueue"
#else
#error "no inotify or kqueue on this platform"
#endif

static void die(const char *what) {
  fprintf(stderr, "notify_probe: %s: %s\n", what, strerror(errno));
  exit(2);
}

/* make_file: create or truncate PATH and write DATA, checking every call. */
static void make_file(const char *path, const char *data) {
  int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  size_t n = strlen(data);
  if (fd < 0) die(path);
  if (write(fd, data, n) != (ssize_t)n) die("write");
  if (close(fd) != 0) die("close");
}

static void join(char *out, size_t size, const char *name) {
  if (out[0] != '\0') strncat(out, "|", size - strlen(out) - 1);
  strncat(out, name, size - strlen(out) - 1);
}

#if defined(__linux__)

static int ino;
static char buf[65536] __attribute__((aligned(__alignof__(struct inotify_event))));

static const struct {
  uint32_t bit;
  const char *name;
} in_names[] = {
  {IN_ACCESS, "ACCESS"}, {IN_MODIFY, "MODIFY"}, {IN_ATTRIB, "ATTRIB"},
  {IN_CLOSE_WRITE, "CLOSE_WRITE"}, {IN_CLOSE_NOWRITE, "CLOSE_NOWRITE"},
  {IN_OPEN, "OPEN"}, {IN_MOVED_FROM, "MOVED_FROM"}, {IN_MOVED_TO, "MOVED_TO"},
  {IN_CREATE, "CREATE"}, {IN_DELETE, "DELETE"}, {IN_DELETE_SELF, "DELETE_SELF"},
  {IN_MOVE_SELF, "MOVE_SELF"}, {IN_UNMOUNT, "UNMOUNT"},
  {IN_Q_OVERFLOW, "Q_OVERFLOW"}, {IN_IGNORED, "IGNORED"}, {IN_ISDIR, "ISDIR"},
};

static const char *mask_str(uint32_t mask) {
  static char out[256];
  size_t i;
  out[0] = '\0';
  for (i = 0; i < sizeof in_names / sizeof in_names[0]; i++)
    if (mask & in_names[i].bit) join(out, sizeof out, in_names[i].name);
  return out;
}

/* Collected events: the probe keeps only what it prints or checks. */
struct seen {
  long count;
  uint32_t last_mask;
  int last_wd;
  uint32_t from_cookie, to_cookie;
  char masks[512];
};

/* drain: read every queued event without blocking (the fd is IN_NONBLOCK). */
static void drain(struct seen *s, int print_names) {
  memset(s, 0, sizeof *s);
  for (;;) {
    ssize_t len = read(ino, buf, sizeof buf);
    char *p;
    if (len < 0) {
      if (errno == EAGAIN) break;
      die("read");
    }
    for (p = buf; p < buf + len;) {
      const struct inotify_event *ev = (const struct inotify_event *)(void *)p;
      s->count++;
      s->last_mask = ev->mask;
      s->last_wd = ev->wd;
      if (ev->mask & IN_MOVED_FROM) s->from_cookie = ev->cookie;
      if (ev->mask & IN_MOVED_TO) s->to_cookie = ev->cookie;
      if (print_names)
        printf("%s %s\n", mask_str(ev->mask), ev->len ? ev->name : "-");
      else if (strlen(s->masks) < sizeof s->masks - 64) {
        if (s->masks[0] != '\0') strcat(s->masks, " ");
        strcat(s->masks, mask_str(ev->mask));
      }
      p += sizeof *ev + ev->len;
    }
  }
}

static int watch(const char *path, uint32_t mask) {
  int wd = inotify_add_watch(ino, path, mask);
  if (wd < 0) die("inotify_add_watch");
  return wd;
}

static void setup(void) {
  ino = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
  if (ino < 0) die("inotify_init1");
}

#else /* kqueue */

static int kq;

static const struct {
  unsigned bit;
  const char *name;
} note_names[] = {
  {NOTE_DELETE, "DELETE"}, {NOTE_WRITE, "WRITE"}, {NOTE_EXTEND, "EXTEND"},
  {NOTE_ATTRIB, "ATTRIB"}, {NOTE_LINK, "LINK"}, {NOTE_RENAME, "RENAME"},
  {NOTE_REVOKE, "REVOKE"},
};

static const char *mask_str(unsigned fflags) {
  static char out[256];
  size_t i;
  out[0] = '\0';
  for (i = 0; i < sizeof note_names / sizeof note_names[0]; i++)
    if (fflags & note_names[i].bit) join(out, sizeof out, note_names[i].name);
  return out;
}

struct seen {
  long count;
  unsigned last_fflags;
  char masks[512];
};

/* drain: collect every pending kevent with a zero timeout. */
static void drain(struct seen *s, int print_names) {
  struct timespec zero = {0, 0};
  struct kevent ev[16];
  int n, i;
  memset(s, 0, sizeof *s);
  for (;;) {
    n = kevent(kq, NULL, 0, ev, 16, &zero);
    if (n < 0) {
      if (errno == EINTR) continue;
      die("kevent");
    }
    if (n == 0) break;
    for (i = 0; i < n; i++) {
      s->count++;
      s->last_fflags = ev[i].fflags;
      if (print_names)
        printf("%s %s\n", mask_str(ev[i].fflags), "-");
      else if (strlen(s->masks) < sizeof s->masks - 64) {
        if (s->masks[0] != '\0') strcat(s->masks, " ");
        strcat(s->masks, mask_str(ev[i].fflags));
      }
    }
  }
}

/* watch: kqueue watches a descriptor, not a name, so open one per watched file. */
static int watch(const char *path, unsigned fflags) {
  struct kevent ch;
#ifdef O_EVTONLY
  int fd = open(path, O_EVTONLY);
#else
  int fd = open(path, O_RDONLY);
#endif
  if (fd < 0) die(path);
  EV_SET(&ch, fd, EVFILT_VNODE, EV_ADD | EV_CLEAR, fflags, 0, 0);
  if (kevent(kq, &ch, 1, NULL, 0, NULL) < 0) die("kevent register");
  return fd;
}

static void setup(void) {
  kq = kqueue();
  if (kq < 0) die("kqueue");
}

#endif

static void path_join(char *out, size_t size, const char *dir, const char *name) {
  if ((size_t)snprintf(out, size, "%s/%s", dir, name) >= size) {
    errno = ENAMETOOLONG;
    die(dir);
  }
}

static void mode_dir(const char *dir) {
  char a[4096], b[4096], sub[4096];
  struct seen s;
  path_join(a, sizeof a, dir, "a.txt");
  path_join(b, sizeof b, dir, "b.txt");
  path_join(sub, sizeof sub, dir, "sub");
#if defined(__linux__)
  watch(dir, IN_CREATE | IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO |
               IN_DELETE | IN_ONLYDIR);
#else
  watch(dir, NOTE_DELETE | NOTE_WRITE | NOTE_EXTEND | NOTE_ATTRIB | NOTE_LINK | NOTE_RENAME);
#endif
  make_file(a, "x\n");
  if (rename(a, b) != 0) die("rename");
  if (mkdir(sub, 0755) != 0) die("mkdir");
  if (unlink(b) != 0) die("unlink");
  if (rmdir(sub) != 0) die("rmdir");
  drain(&s, 1);
  printf("events: %ld\n", s.count);
#if defined(__linux__)
  printf("rename cookies: %s\n",
         (s.from_cookie != 0 && s.from_cookie == s.to_cookie) ? "equal" : "differ");
#endif
}

static void mode_coalesce(const char *file) {
  struct seen s;
  long paired = 0;
  int fd, i;
  make_file(file, "");
#if defined(__linux__)
  watch(file, IN_MODIFY);
#else
  watch(file, NOTE_WRITE | NOTE_EXTEND);
#endif
  fd = open(file, O_WRONLY | O_APPEND);
  if (fd < 0) die(file);
  for (i = 0; i < 5; i++)
    if (write(fd, "x", 1) != 1) die("write");
  drain(&s, 0);
  printf("5 writes, read once: %ld event(s) [%s]\n", s.count, s.masks);
  for (i = 0; i < 2; i++) {
    if (write(fd, "y", 1) != 1) die("write");
    drain(&s, 0);
    paired += s.count;
  }
  printf("2 writes, read after each: %ld event(s)\n", paired);
  if (close(fd) != 0) die("close");
}

static void mode_overflow(const char *dir) {
  char f[4096];
  struct seen s;
  long pairs, i, limit;
  path_join(f, sizeof f, dir, "f");
#if defined(__linux__)
  {
    FILE *lim = fopen("/proc/sys/fs/inotify/max_queued_events", "r");
    if (lim == NULL) die("/proc/sys/fs/inotify/max_queued_events");
    if (fscanf(lim, "%ld", &limit) != 1) {
      errno = EINVAL;
      die("max_queued_events");
    }
    fclose(lim);
  }
  watch(dir, IN_CREATE | IN_DELETE);
  pairs = limit / 2 + 100;
#else
  limit = 0;
  (void)limit; /* kqueue has no queue limit to read */
  watch(dir, NOTE_WRITE | NOTE_LINK);
  pairs = 10000;
#endif
  for (i = 0; i < pairs; i++) {
    make_file(f, "");
    if (unlink(f) != 0) die("unlink");
  }
  drain(&s, 0);
#if defined(__linux__)
  printf("max_queued_events: %ld\n", limit);
  printf("generated: limit + %ld\n", 2 * pairs - limit);
  printf("read: limit + %ld\n", s.count - limit);
  printf("last: %s wd=%d\n", mask_str(s.last_mask), s.last_wd);
#else
  printf("generated: %ld\n", 2 * pairs);
  printf("read: %ld\n", s.count);
  printf("last: %s\n", mask_str(s.last_fflags));
#endif
}

static void mode_replace(const char *dir) {
  char target[4096], tmp[4096];
  struct seen s;
  path_join(target, sizeof target, dir, "target");
  path_join(tmp, sizeof tmp, dir, "target.tmp");
  make_file(target, "v1\n");
#if defined(__linux__)
  watch(target, IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_DELETE_SELF | IN_MOVE_SELF);
#else
  watch(target, NOTE_DELETE | NOTE_WRITE | NOTE_EXTEND | NOTE_ATTRIB | NOTE_LINK | NOTE_RENAME);
#endif
  make_file(tmp, "v2\n");
  if (rename(tmp, target) != 0) die("rename");
  drain(&s, 0);
  printf("after rename over target: %s\n", s.count ? s.masks : "none");
  make_file(target, "v3\n");
  drain(&s, 0);
  printf("after rewriting the new target: %s\n", s.count ? s.masks : "none");
}

int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "backend") == 0) {
    printf("%s\n", BACKEND);
    return 0;
  }
  if (argc != 3) {
    fprintf(stderr, "usage: notify_probe backend | dir|coalesce|overflow|replace PATH\n");
    return 2;
  }
  setup();
  if (strcmp(argv[1], "dir") == 0) mode_dir(argv[2]);
  else if (strcmp(argv[1], "coalesce") == 0) mode_coalesce(argv[2]);
  else if (strcmp(argv[1], "overflow") == 0) mode_overflow(argv[2]);
  else if (strcmp(argv[1], "replace") == 0) mode_replace(argv[2]);
  else {
    fprintf(stderr, "notify_probe: unknown mode %s\n", argv[1]);
    return 2;
  }
  return 0;
}
