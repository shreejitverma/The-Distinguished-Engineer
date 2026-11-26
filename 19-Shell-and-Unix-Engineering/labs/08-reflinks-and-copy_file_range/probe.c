/*
 * probe.c: report what the kernel does for reflink clones and copy_file_range.
 *
 * Modes (each prints one line on stdout and exits 0 unless its arguments are bad):
 *   fstype PATH       file system type of PATH: apfs, btrfs, xfs, ext4, tmpfs, overlay, ...
 *   clone SRC DST     whole-file clone: clonefile(2) on Darwin, ioctl(FICLONE) on Linux;
 *                     prints "clone ok" or "clone ERRNO-NAME"
 *   shared FILE       does FILE share its blocks with another file? Darwin reads
 *                     ATTR_CMNEXT_EXT_FLAGS, Linux reads FS_IOC_FIEMAP extent flags;
 *                     prints "shared all", "shared some", "shared none", or "shared ERRNO-NAME"
 *   cfr SRC DST       copy SRC to a new DST with copy_file_range(2) in a loop;
 *                     prints "cfr ok bytes=N calls=K", "cfr ERRNO-NAME bytes=N", or
 *                     "cfr absent" when the C library has no copy_file_range symbol
 *   cfr-append SRC DST  the same, but DST is opened with O_APPEND
 *
 * copy_file_range is looked up with dlsym at run time, so the probe compiles on every
 * platform and the "absent" answer is computed, not assumed.
 */
#define _GNU_SOURCE 1
#define _FILE_OFFSET_BITS 64
#if defined(__APPLE__)
#define _DARWIN_C_SOURCE 1
#endif

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <sys/attr.h>
#include <sys/clonefile.h>
#include <sys/mount.h> /* statfs on Darwin; glibc sys/mount.h clashes with linux/fs.h */
#endif

#if defined(__linux__)
#include <sys/vfs.h>
#if defined(__has_include)
#if __has_include(<linux/fiemap.h>) && __has_include(<linux/fs.h>)
#define PROBE_HAVE_LINUX_HEADERS 1
#endif
#endif
#if defined(PROBE_HAVE_LINUX_HEADERS)
#include <linux/fiemap.h>
#include <linux/fs.h>
#else
/*
 * Images without kernel headers (for example Alpine without linux-headers) lack these.
 * The values are the stable kernel ABI from include/uapi/linux/fiemap.h and fs.h.
 */
struct fiemap_extent {
  uint64_t fe_logical;
  uint64_t fe_physical;
  uint64_t fe_length;
  uint64_t fe_reserved64[2];
  uint32_t fe_flags;
  uint32_t fe_reserved[3];
};
struct fiemap {
  uint64_t fm_start;
  uint64_t fm_length;
  uint32_t fm_flags;
  uint32_t fm_mapped_extents;
  uint32_t fm_extent_count;
  uint32_t fm_reserved;
  struct fiemap_extent fm_extents[];
};
#define FIEMAP_FLAG_SYNC 0x00000001
#define FIEMAP_EXTENT_SHARED 0x00002000
#define FS_IOC_FIEMAP _IOWR('f', 11, struct fiemap)
#define FICLONE _IOW(0x94, 9, int)
#endif
#endif

static const char *errname(int e) {
  switch (e) {
    case EBADF: return "EBADF";
    case EEXIST: return "EEXIST";
    case EINVAL: return "EINVAL";
    case EISDIR: return "EISDIR";
    case ENOENT: return "ENOENT";
    case ENOSPC: return "ENOSPC";
    case ENOSYS: return "ENOSYS";
    case ENOTTY: return "ENOTTY";
    case EPERM: return "EPERM";
    case EXDEV: return "EXDEV";
#if defined(EOPNOTSUPP)
    case EOPNOTSUPP: return "EOPNOTSUPP";
#endif
#if defined(ENOTSUP) && (!defined(EOPNOTSUPP) || ENOTSUP != EOPNOTSUPP)
    case ENOTSUP: return "ENOTSUP";
#endif
    default: return "EOTHER";
  }
}

static int mode_fstype(const char *path) {
#if defined(__APPLE__)
  struct statfs sfs;
  if (statfs(path, &sfs) != 0) {
    printf("fstype %s\n", errname(errno));
    return 0;
  }
  printf("fstype %s\n", sfs.f_fstypename);
#elif defined(__linux__)
  struct statfs sfs;
  const char *name = "other";
  if (statfs(path, &sfs) != 0) {
    printf("fstype %s\n", errname(errno));
    return 0;
  }
  /* Magic numbers from include/uapi/linux/magic.h. */
  switch ((unsigned long)sfs.f_type) {
    case 0x9123683EUL: name = "btrfs"; break;
    case 0x58465342UL: name = "xfs"; break;
    case 0xEF53UL: name = "ext4"; break;
    case 0x01021994UL: name = "tmpfs"; break;
    case 0x794c7630UL: name = "overlay"; break;
    case 0xca451a4eUL: name = "bcachefs"; break;
    case 0x65735546UL: name = "fuse"; break;
    case 0x01021997UL: name = "9p"; break;
    default: break;
  }
  printf("fstype %s\n", name);
#else
  (void)path;
  printf("fstype other\n");
#endif
  return 0;
}

static int mode_clone(const char *src, const char *dst) {
#if defined(__APPLE__)
  /* clonefile(2) creates DST itself and fails with EEXIST if it exists. */
  if (clonefile(src, dst, 0) != 0) {
    printf("clone %s\n", errname(errno));
    return 0;
  }
  printf("clone ok\n");
#elif defined(__linux__) && defined(FICLONE)
  /* FICLONE needs an open destination; an existing one is replaced, like cp does. */
  int in = open(src, O_RDONLY);
  int out;
  if (in < 0) {
    printf("clone %s\n", errname(errno));
    return 0;
  }
  out = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (out < 0) {
    printf("clone %s\n", errname(errno));
    close(in);
    return 0;
  }
  if (ioctl(out, FICLONE, in) != 0) {
    printf("clone %s\n", errname(errno));
  } else {
    printf("clone ok\n");
  }
  close(out);
  close(in);
#else
  (void)src;
  (void)dst;
  printf("clone %s\n", errname(ENOSYS));
#endif
  return 0;
}

static int mode_shared(const char *path) {
#if defined(__APPLE__)
  struct attrlist al;
  struct {
    uint32_t length;
    attribute_set_t returned;
    uint64_t ext_flags;
  } __attribute__((packed)) buf;
  memset(&al, 0, sizeof al);
  memset(&buf, 0, sizeof buf);
  al.bitmapcount = ATTR_BIT_MAP_COUNT;
  al.commonattr = ATTR_CMN_RETURNED_ATTRS;
  al.forkattr = ATTR_CMNEXT_EXT_FLAGS;
  if (getattrlist(path, &al, &buf, sizeof buf, FSOPT_ATTR_CMN_EXTENDED) != 0) {
    printf("shared %s\n", errname(errno));
    return 0;
  }
  if (!(buf.returned.forkattr & ATTR_CMNEXT_EXT_FLAGS)) {
    printf("shared %s\n", errname(ENOTSUP));
  } else if (buf.ext_flags & EF_SHARES_ALL_BLOCKS) {
    printf("shared all\n");
  } else if (buf.ext_flags & EF_MAY_SHARE_BLOCKS) {
    printf("shared some\n");
  } else {
    printf("shared none\n");
  }
#elif defined(__linux__)
  enum { NEXT = 64 };
  size_t size = sizeof(struct fiemap) + NEXT * sizeof(struct fiemap_extent);
  struct fiemap *fm = calloc(1, size);
  unsigned int i, nshared = 0;
  int fd;
  if (fm == NULL) {
    printf("shared %s\n", errname(errno));
    return 1;
  }
  fd = open(path, O_RDONLY);
  if (fd < 0) {
    printf("shared %s\n", errname(errno));
    free(fm);
    return 0;
  }
  fm->fm_start = 0;
  fm->fm_length = ~0ULL;
  fm->fm_flags = FIEMAP_FLAG_SYNC; /* flush delayed allocation so extents are real */
  fm->fm_extent_count = NEXT;
  if (ioctl(fd, FS_IOC_FIEMAP, fm) != 0) {
    printf("shared %s\n", errname(errno));
  } else {
    for (i = 0; i < fm->fm_mapped_extents; i++) {
      if (fm->fm_extents[i].fe_flags & FIEMAP_EXTENT_SHARED) {
        nshared++;
      }
    }
    if (fm->fm_mapped_extents > 0 && nshared == fm->fm_mapped_extents) {
      printf("shared all\n");
    } else if (nshared > 0) {
      printf("shared some\n");
    } else {
      printf("shared none\n");
    }
  }
  close(fd);
  free(fm);
#else
  (void)path;
  printf("shared %s\n", errname(ENOSYS));
#endif
  return 0;
}

typedef ssize_t (*cfr_fn)(int, off_t *, int, off_t *, size_t, unsigned int);

static int mode_cfr(const char *src, const char *dst, int append) {
  void *sym = dlsym(RTLD_DEFAULT, "copy_file_range");
  cfr_fn cfr;
  long long total = 0;
  long calls = 0;
  int in, out;
  if (sym == NULL) {
    printf("cfr absent\n");
    return 0;
  }
  /* Converting a data pointer to a function pointer is how dlsym is meant to be used (POSIX). */
  memcpy(&cfr, &sym, sizeof cfr);
  in = open(src, O_RDONLY);
  if (in < 0) {
    printf("cfr %s bytes=0\n", errname(errno));
    return 0;
  }
  out = open(dst, O_WRONLY | O_CREAT | O_TRUNC | (append ? O_APPEND : 0), 0644);
  if (out < 0) {
    printf("cfr %s bytes=0\n", errname(errno));
    close(in);
    return 0;
  }
  for (;;) {
    /* Ask for 1 GiB per call; the kernel may return less (it caps a call at MAX_RW_COUNT). */
    ssize_t n = cfr(in, NULL, out, NULL, (size_t)1 << 30, 0);
    calls++;
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      printf("cfr %s bytes=%lld\n", errname(errno), total);
      close(out);
      close(in);
      return 0;
    }
    if (n == 0) {
      break; /* EOF of SRC, or a file that lies about its size (see the note) */
    }
    total += n;
  }
  printf("cfr ok bytes=%lld calls=%ld\n", total, calls);
  close(out);
  close(in);
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 3 && strcmp(argv[1], "fstype") == 0) {
    return mode_fstype(argv[2]);
  }
  if (argc == 4 && strcmp(argv[1], "clone") == 0) {
    return mode_clone(argv[2], argv[3]);
  }
  if (argc == 3 && strcmp(argv[1], "shared") == 0) {
    return mode_shared(argv[2]);
  }
  if (argc == 4 && strcmp(argv[1], "cfr") == 0) {
    return mode_cfr(argv[2], argv[3], 0);
  }
  if (argc == 4 && strcmp(argv[1], "cfr-append") == 0) {
    return mode_cfr(argv[2], argv[3], 1);
  }
  fprintf(stderr, "usage: probe fstype PATH | clone SRC DST | shared FILE | cfr SRC DST | cfr-append SRC DST\n");
  return 2;
}
