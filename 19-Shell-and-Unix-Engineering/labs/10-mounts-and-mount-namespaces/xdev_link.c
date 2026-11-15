/* xdev_link.c - call link(2) and report the outcome by errno name.
 * Usage: xdev_link SOURCE NEWPATH
 * Prints "link=ok", "link=EXDEV", "link=EPERM", or "link=errno:N".
 * Linux tests the mounts first and returns EXDEV whenever the two paths are on
 * different mounts (fs/namei.c, filename_linkat: old_path.mnt != new_path.mnt),
 * even two bind mounts of one filesystem. XNU runs its MAC and kauth link checks
 * on the source before its cross-mount test (bsd/vfs/vfs_syscalls.c,
 * linkat_internal), so a source the caller may not link gives EPERM there.
 */
#include <errno.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: xdev_link SOURCE NEWPATH\n");
    return 2;
  }
  if (link(argv[1], argv[2]) == 0) {
    printf("link=ok\n");
    return 0;
  }
  if (errno == EXDEV) {
    printf("link=EXDEV\n");
  } else if (errno == EPERM) {
    printf("link=EPERM\n");
  } else {
    printf("link=errno:%d\n", errno);
  }
  return 1;
}
