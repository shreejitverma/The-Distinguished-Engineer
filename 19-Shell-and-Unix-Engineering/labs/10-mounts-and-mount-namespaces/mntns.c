/* mntns.c - mount namespace probes (Linux only).
 * Usage:
 *   mntns inherit      fork a child and compare its /proc/self/ns/mnt with the parent's
 *   mntns isolate DIR  in a child: unshare(CLONE_NEWUSER | CLONE_NEWNS), map root, make every
 *                      mount private, mount a tmpfs on DIR and create DIR/inside; then the
 *                      parent checks whether it can see DIR/inside.
 * On a kernel without mount namespaces (macOS, BSD) it prints "mntns: unsupported".
 * isolate prints "mntns=new child_sees=yes parent_sees=no" when the kernel grants an
 * unprivileged namespace, else "failed_at=STAGE errno=NAME parent_sees=no".
 * Every wait is bounded: the child exits as soon as its pipe to the parent closes.
 */
#ifdef __linux__
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *errname(int e) {
  switch (e) {
    case EPERM: return "EPERM";
    case EACCES: return "EACCES";
    case EINVAL: return "EINVAL";
    case ENOSPC: return "ENOSPC";
    case ENOSYS: return "ENOSYS";
    case EUSERS: return "EUSERS";
    case ENOENT: return "ENOENT";
    default: return "OTHER";
  }
}

static int ns_inode(ino_t *out) {
  struct stat st;
  if (stat("/proc/self/ns/mnt", &st) != 0) {
    return -1;
  }
  *out = st.st_ino;
  return 0;
}

static int write_file(const char *path, const char *text) {
  int fd = open(path, O_WRONLY | O_CLOEXEC);
  ssize_t len = (ssize_t)strlen(text);
  ssize_t n;
  if (fd < 0) {
    return -1;
  }
  n = write(fd, text, (size_t)len);
  if (close(fd) != 0 || n != len) {
    return -1;
  }
  return 0;
}

/* Child side of isolate: report "ok" or "STAGE ERRNO" on fd, then wait for the parent. */
static void isolate_child(const char *dir, ino_t parent_ns, int report, int go) {
  char msg[128];
  char map[64];
  char inside[4096];
  const char *stage = NULL;
  int fd;
  ino_t mine;
  char byte;
  ssize_t n;

  if (unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0) {
    stage = "unshare";
  } else if (write_file("/proc/self/setgroups", "deny") != 0 && errno != ENOENT) {
    stage = "setgroups";
  } else if (snprintf(map, sizeof map, "0 %ld 1\n", (long)geteuid()) < 0 ||
             write_file("/proc/self/uid_map", map) != 0) {
    stage = "uid_map";
  } else if (snprintf(map, sizeof map, "0 %ld 1\n", (long)getegid()) < 0 ||
             write_file("/proc/self/gid_map", map) != 0) {
    stage = "gid_map";
  } else if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) != 0) {
    stage = "make-rprivate";
  } else if (mount("labfs", dir, "tmpfs", 0, "size=64k") != 0) {
    stage = "mount";
  }
  if (stage != NULL) {
    snprintf(msg, sizeof msg, "%s %s\n", stage, errname(errno));
  } else {
    snprintf(inside, sizeof inside, "%s/inside", dir);
    fd = open(inside, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0 || close(fd) != 0) {
      snprintf(msg, sizeof msg, "create %s\n", errname(errno));
    } else if (ns_inode(&mine) != 0) {
      snprintf(msg, sizeof msg, "nsread %s\n", errname(errno));
    } else {
      snprintf(msg, sizeof msg, "ok %s %s\n", mine != parent_ns ? "new" : "same",
               access(inside, F_OK) == 0 ? "yes" : "no");
    }
  }
  n = write(report, msg, strlen(msg));
  if (n < 0) {
    _exit(3); /* the parent then reads EOF and reports a failure */
  }
  close(report);
  /* Keep the namespace (and its mount) alive until the parent has looked. */
  n = read(go, &byte, 1);
  _exit(n < 0 ? 3 : 0);
}

static int isolate(const char *dir) {
  int to_parent[2], to_child[2];
  ino_t parent_ns;
  pid_t pid;
  char buf[128];
  char stage[64], err[64], ns[16], sees[16];
  char inside[4096];
  ssize_t n;
  int status;
  const char *parent_sees;

  if (ns_inode(&parent_ns) != 0) {
    perror("stat /proc/self/ns/mnt");
    return 1;
  }
  if (pipe(to_parent) != 0 || pipe(to_child) != 0) {
    perror("pipe");
    return 1;
  }
  pid = fork();
  if (pid < 0) {
    perror("fork");
    return 1;
  }
  if (pid == 0) {
    close(to_parent[0]);
    close(to_child[1]);
    isolate_child(dir, parent_ns, to_parent[1], to_child[0]);
  }
  close(to_parent[1]);
  close(to_child[0]);
  n = read(to_parent[0], buf, sizeof buf - 1);
  buf[n > 0 ? n : 0] = '\0';
  snprintf(inside, sizeof inside, "%s/inside", dir);
  parent_sees = access(inside, F_OK) == 0 ? "yes" : "no";
  close(to_child[1]); /* releases the child */
  if (waitpid(pid, &status, 0) != pid) {
    perror("waitpid");
    return 1;
  }
  if (sscanf(buf, "ok %15s %15s", ns, sees) == 2) {
    printf("mntns=%s child_sees=%s parent_sees=%s\n", ns, sees, parent_sees);
    return 0;
  }
  if (sscanf(buf, "%63s %63s", stage, err) == 2) {
    printf("failed_at=%s errno=%s parent_sees=%s\n", stage, err, parent_sees);
  } else {
    printf("failed_at=child errno=OTHER parent_sees=%s\n", parent_sees);
  }
  return 3;
}

static int inherit(void) {
  ino_t parent_ns, child_ns;
  pid_t pid;
  int status;

  if (ns_inode(&parent_ns) != 0) {
    perror("stat /proc/self/ns/mnt");
    return 1;
  }
  pid = fork();
  if (pid < 0) {
    perror("fork");
    return 1;
  }
  if (pid == 0) {
    _exit(ns_inode(&child_ns) == 0 && child_ns == parent_ns ? 0 : 1);
  }
  if (waitpid(pid, &status, 0) != pid) {
    perror("waitpid");
    return 1;
  }
  printf("child_shares_parent_mntns=%s\n",
         WIFEXITED(status) && WEXITSTATUS(status) == 0 ? "yes" : "no");
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "inherit") == 0) {
    return inherit();
  }
  if (argc == 3 && strcmp(argv[1], "isolate") == 0) {
    return isolate(argv[2]);
  }
  fprintf(stderr, "usage: mntns inherit | mntns isolate DIR\n");
  return 2;
}
#else
#include <stdio.h>

int main(void) {
  printf("mntns: unsupported\n");
  return 0;
}
#endif
