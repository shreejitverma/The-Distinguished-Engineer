/* rename_probe MODE OLD NEW: call the kernel's rename directly and print "ok" or the
 * errno name, so a test can separate what rename(2) does from what mv(1) decides.
 *
 *   plain      rename(OLD, NEW)
 *   noreplace  fail with EEXIST if NEW exists: Linux renameat2(RENAME_NOREPLACE),
 *              macOS renamex_np(RENAME_EXCL)
 *   exchange   swap OLD and NEW atomically: Linux renameat2(RENAME_EXCHANGE),
 *              macOS renamex_np(RENAME_SWAP)
 *
 * Exit status: 0 when the call succeeded, 1 when it failed, 2 on a usage error.
 */
#if defined(__linux__)
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/syscall.h>
/* Values from include/uapi/linux/fs.h; defined here because older libcs lack them. */
#ifndef RENAME_NOREPLACE
#define RENAME_NOREPLACE (1 << 0)
#endif
#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1 << 1)
#endif
/* Call the system call directly: glibc wraps renameat2 only since 2.28. */
static int rename_flags(const char *from, const char *to, unsigned int flags) {
  return (int)syscall(SYS_renameat2, AT_FDCWD, from, AT_FDCWD, to, flags);
}
#define FLAG_NOREPLACE RENAME_NOREPLACE
#define FLAG_EXCHANGE RENAME_EXCHANGE
#elif defined(__APPLE__)
static int rename_flags(const char *from, const char *to, unsigned int flags) {
  return renamex_np(from, to, flags);
}
#define FLAG_NOREPLACE RENAME_EXCL
#define FLAG_EXCHANGE RENAME_SWAP
#else
#error "rename_probe supports Linux (renameat2) and macOS (renamex_np) only"
#endif

static const char *errno_name(int e) {
  switch (e) {
  case EEXIST: return "EEXIST";
  case ENOENT: return "ENOENT";
  case EINVAL: return "EINVAL";
  case ENOSYS: return "ENOSYS";
  case EXDEV: return "EXDEV";
  case ENOTDIR: return "ENOTDIR";
  case EISDIR: return "EISDIR";
  case ENOTEMPTY: return "ENOTEMPTY";
  default: return "OTHER";
  }
}

int main(int argc, char **argv) {
  int r;
  if (argc != 4) {
    fprintf(stderr, "usage: rename_probe plain|noreplace|exchange OLD NEW\n");
    return 2;
  }
  if (strcmp(argv[1], "plain") == 0) {
    r = rename(argv[2], argv[3]);
  } else if (strcmp(argv[1], "noreplace") == 0) {
    r = rename_flags(argv[2], argv[3], FLAG_NOREPLACE);
  } else if (strcmp(argv[1], "exchange") == 0) {
    r = rename_flags(argv[2], argv[3], FLAG_EXCHANGE);
  } else {
    fprintf(stderr, "rename_probe: unknown mode %s\n", argv[1]);
    return 2;
  }
  if (r == 0) {
    puts("ok");
    return 0;
  }
  puts(errno_name(errno));
  return 1;
}
