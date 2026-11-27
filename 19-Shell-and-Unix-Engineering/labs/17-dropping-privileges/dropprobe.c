/* dropprobe.c - inspect Unix process credentials, privilege transitions,
 * descriptor leakage, and kernel security bits across exec boundaries.
 * Usage: dropprobe MODE [ARG...]
 * Modes:
 *   ids         - print real, effective, and supplementary group IDs
 *   try-root    - attempt setuid(0) and seteuid(0), printing result and errno
 *   order-check - attempt setgroups(0, NULL) as unprivileged process
 *   fdcheck FD  - check if file descriptor FD is open or closed
 *   nonewprivs  - test PR_SET_NO_NEW_PRIVS where supported (Linux)
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/prctl.h>
#ifndef PR_SET_NO_NEW_PRIVS
#define PR_SET_NO_NEW_PRIVS 38
#endif
#ifndef PR_GET_NO_NEW_PRIVS
#define PR_GET_NO_NEW_PRIVS 39
#endif
#endif

static const char *errno_to_name(int err)
{
  switch (err) {
  case 0:
    return "SUCCESS";
  case EPERM:
    return "EPERM";
  case EACCES:
    return "EACCES";
  case EINVAL:
    return "EINVAL";
  case EAGAIN:
    return "EAGAIN";
  case EBADF:
    return "EBADF";
  default:
    return "OTHER_ERROR";
  }
}

static int do_ids(void)
{
  gid_t groups[64];
  int ngroups;
  uid_t ruid = getuid();
  uid_t euid = geteuid();
  gid_t rgid = getgid();
  gid_t egid = getegid();

  ngroups = getgroups(64, groups);
  if (ngroups < 0) {
    fprintf(stderr, "getgroups failed: %s\n", strerror(errno));
    return 1;
  }

#if defined(__linux__) || defined(__FreeBSD__) || defined(__OpenBSD__)
  uid_t s_uid;
  gid_t s_gid;
  uid_t r_uid, e_uid;
  gid_t r_gid, e_gid;
  if (getresuid(&r_uid, &e_uid, &s_uid) == 0 &&
      getresgid(&r_gid, &e_gid, &s_gid) == 0) {
    printf("ruid=%d euid=%d suid=%d rgid=%d egid=%d sgid=%d groups=%d\n",
           (int)r_uid, (int)e_uid, (int)s_uid,
           (int)r_gid, (int)e_gid, (int)s_gid, ngroups);
    return 0;
  }
#endif

  printf("ruid=%d euid=%d rgid=%d egid=%d groups=%d\n",
         (int)ruid, (int)euid, (int)rgid, (int)egid, ngroups);
  return 0;
}

static int do_try_root(void)
{
  int r_uid;
  int err_uid;
  int r_euid;
  int err_euid;
  int r_gid;
  int err_gid;

  errno = 0;
  r_uid = setuid(0);
  err_uid = errno;

  errno = 0;
  r_euid = seteuid(0);
  err_euid = errno;

  errno = 0;
  r_gid = setgid(0);
  err_gid = errno;

  printf("setuid(0): ret=%d errno=%s\n", r_uid, errno_to_name(err_uid));
  printf("seteuid(0): ret=%d errno=%s\n", r_euid, errno_to_name(err_euid));
  printf("setgid(0): ret=%d errno=%s\n", r_gid, errno_to_name(err_gid));
  return (r_uid == 0 || r_euid == 0 || r_gid == 0) ? 1 : 0;
}

static int do_order_check(void)
{
  int ret;
  int err;

  errno = 0;
#if defined(__APPLE__)
  /* On macOS, passing a NULL array pointer can yield EFAULT instead of EPERM */
  gid_t dummy = 0;
  ret = setgroups(0, &dummy);
#else
  ret = setgroups(0, NULL);
#endif
  err = errno;

  printf("setgroups(0): ret=%d errno=%s\n", ret, errno_to_name(err));
  return (ret == 0) ? 1 : 0;
}

static int do_fdcheck(int fd)
{
  int flags = fcntl(fd, F_GETFD);
  if (flags >= 0) {
    printf("fd %d: open flags=%d\n", fd, flags);
    return 0;
  }
  printf("fd %d: closed errno=%s\n", fd, errno_to_name(errno));
  return 1;
}

static int do_nonewprivs(void)
{
#if defined(__linux__)
  int r_set = prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
  if (r_set != 0) {
    printf("nonewprivs: set_failed errno=%s\n", errno_to_name(errno));
    return 1;
  }
  int r_get = prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0);
  printf("nonewprivs: active=%d\n", r_get);
  return (r_get == 1) ? 0 : 1;
#else
  printf("nonewprivs: unsupported\n");
  return 0;
#endif
}

int main(int argc, char **argv)
{
  if (argc < 2) {
    fprintf(stderr, "usage: dropprobe MODE [ARG...]\n");
    return 2;
  }

  if (strcmp(argv[1], "ids") == 0) {
    return do_ids();
  }
  if (strcmp(argv[1], "try-root") == 0) {
    return do_try_root();
  }
  if (strcmp(argv[1], "order-check") == 0) {
    return do_order_check();
  }
  if (strcmp(argv[1], "fdcheck") == 0) {
    if (argc < 3) {
      fprintf(stderr, "usage: dropprobe fdcheck FD\n");
      return 2;
    }
    return do_fdcheck(atoi(argv[2]));
  }
  if (strcmp(argv[1], "nonewprivs") == 0) {
    return do_nonewprivs();
  }

  fprintf(stderr, "dropprobe: unknown mode %s\n", argv[1]);
  return 2;
}
