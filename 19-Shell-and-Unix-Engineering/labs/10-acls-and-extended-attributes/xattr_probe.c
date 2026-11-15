/*
 * xattr_probe: call the extended-attribute system calls directly and print
 * what the kernel answered, so a test can see the mechanism without getfattr,
 * setfacl, or xattr(1), which the matrix images do not ship.
 *
 * Usage:
 *   xattr_probe set FILE NAME VALUE      setxattr; prints "ok" or an errno name
 *   xattr_probe get FILE NAME            getxattr; prints the value or an errno name
 *   xattr_probe list FILE PREFIX         listxattr; prints names that start with PREFIX
 *   xattr_probe size FILE NAME BYTES     setxattr with a BYTES-long value of 'x'
 *   xattr_probe namelen FILE LEN         setxattr with a LEN-byte name "user.aaa..."
 *   xattr_probe acl-set FILE U NU G M O  Linux: write system.posix_acl_access with
 *                                        user::U user:EUID:NU group::G mask::M other::O
 *                                        (each an octal digit 0-7)
 *   xattr_probe acl-dump FILE            Linux: decode system.posix_acl_access
 *
 * Exit status: 0 when the call succeeded, 1 when the kernel refused it,
 * 2 for a usage error or a mode this kernel does not have.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifdef __APPLE__
/* Darwin adds a position and an options argument to every call. */
#define P_SET(p, n, v, s) setxattr((p), (n), (v), (s), 0, 0)
#define P_GET(p, n, v, s) getxattr((p), (n), (v), (s), 0, 0)
#define P_LIST(p, b, s) listxattr((p), (b), (s), 0)
#else
#define P_SET(p, n, v, s) setxattr((p), (n), (v), (s), 0)
#define P_GET(p, n, v, s) getxattr((p), (n), (v), (s))
#define P_LIST(p, b, s) listxattr((p), (b), (s))
#endif

static const char *errno_name(int e) {
  switch (e) {
  case E2BIG: return "E2BIG";
  case ERANGE: return "ERANGE";
  case ENAMETOOLONG: return "ENAMETOOLONG";
  case ENOSPC: return "ENOSPC";
  case EPERM: return "EPERM";
  case EACCES: return "EACCES";
  case EINVAL: return "EINVAL";
  case EEXIST: return "EEXIST";
  case ENOENT: return "ENOENT";
#ifdef __APPLE__
  /* Darwin reports a missing attribute as ENOATTR. */
  case ENOATTR: return "ENOATTR";
#else
  /* Linux reports it as ENODATA (some headers alias ENOATTR to it). */
  case ENODATA: return "ENODATA";
#endif
  case ENOTSUP: return "ENOTSUP";
#if defined(EOPNOTSUPP) && EOPNOTSUPP != ENOTSUP
  case EOPNOTSUPP: return "EOPNOTSUPP";
#endif
  default: return "EOTHER";
  }
}

static int report(long r) {
  if (r < 0) {
    printf("%s\n", errno_name(errno));
    return 1;
  }
  printf("ok\n");
  return 0;
}

static int do_get(const char *path, const char *name) {
  char buf[65536];
  ssize_t n = P_GET(path, name, buf, sizeof buf);
  if (n < 0) {
    printf("%s\n", errno_name(errno));
    return 1;
  }
  printf("%.*s\n", (int)n, buf);
  return 0;
}

static int do_list(const char *path, const char *prefix) {
  char buf[65536];
  ssize_t n = P_LIST(path, buf, sizeof buf);
  ssize_t i = 0;
  size_t plen = strlen(prefix);
  if (n < 0) {
    printf("%s\n", errno_name(errno));
    return 1;
  }
  /* The list is a run of NUL-terminated names. */
  while (i < n) {
    const char *name = buf + i;
    size_t len = strlen(name);
    if (strncmp(name, prefix, plen) == 0) {
      printf("%s\n", name);
    }
    i += (ssize_t)len + 1;
  }
  return 0;
}

static int do_size(const char *path, const char *name, const char *bytes) {
  char *end = NULL;
  unsigned long size = strtoul(bytes, &end, 10);
  char *val;
  int rc;
  if (*bytes == '\0' || *end != '\0' || size > 64UL * 1024 * 1024) {
    fprintf(stderr, "xattr_probe: bad size %s\n", bytes);
    return 2;
  }
  val = malloc(size ? size : 1);
  if (val == NULL) {
    perror("malloc");
    return 2;
  }
  memset(val, 'x', size);
  rc = report(P_SET(path, name, val, size));
  free(val);
  return rc;
}

static int do_namelen(const char *path, const char *lenarg) {
  char *end = NULL;
  unsigned long len = strtoul(lenarg, &end, 10);
  char name[1024];
  if (*lenarg == '\0' || *end != '\0' || len < 6 || len >= sizeof name) {
    fprintf(stderr, "xattr_probe: bad name length %s\n", lenarg);
    return 2;
  }
  memcpy(name, "user.", 5);
  memset(name + 5, 'a', len - 5);
  name[len] = '\0';
  return report(P_SET(path, name, "v", 1));
}

#ifdef __linux__
/* The on-disk and on-wire format from include/uapi/linux/posix_acl_xattr.h:
 * a little-endian 32-bit version (2), then 8-byte entries of
 * le16 e_tag, le16 e_perm, le32 e_id, sorted by tag and then by id. */
enum { TAG_USER_OBJ = 0x01, TAG_USER = 0x02, TAG_GROUP_OBJ = 0x04,
       TAG_GROUP = 0x08, TAG_MASK = 0x10, TAG_OTHER = 0x20 };
static const char ACL_NAME[] = "system.posix_acl_access";

static void put16(unsigned char *p, unsigned v) {
  p[0] = (unsigned char)(v & 0xff);
  p[1] = (unsigned char)((v >> 8) & 0xff);
}

static void put32(unsigned char *p, unsigned long v) {
  put16(p, (unsigned)(v & 0xffff));
  put16(p + 2, (unsigned)((v >> 16) & 0xffff));
}

static unsigned get16(const unsigned char *p) { return p[0] | (unsigned)p[1] << 8; }

static unsigned long get32(const unsigned char *p) {
  return get16(p) | (unsigned long)get16(p + 2) << 16;
}

static int perm_digit(const char *s) {
  if (s[0] < '0' || s[0] > '7' || s[1] != '\0') {
    return -1;
  }
  return s[0] - '0';
}

static int do_acl_set(const char *path, char **perms) {
  static const unsigned tags[5] = {TAG_USER_OBJ, TAG_USER, TAG_GROUP_OBJ, TAG_MASK, TAG_OTHER};
  unsigned char buf[4 + 5 * 8];
  int i;
  put32(buf, 2);
  for (i = 0; i < 5; i++) {
    int perm = perm_digit(perms[i]);
    unsigned char *e = buf + 4 + 8 * i;
    if (perm < 0) {
      fprintf(stderr, "xattr_probe: permission %s is not an octal digit\n", perms[i]);
      return 2;
    }
    put16(e, tags[i]);
    put16(e + 2, (unsigned)perm);
    /* ACL_UNDEFINED_ID (-1) for the entries that carry no qualifier. */
    put32(e + 4, tags[i] == TAG_USER ? (unsigned long)geteuid() : 0xffffffffUL);
  }
  return report(P_SET(path, ACL_NAME, buf, sizeof buf));
}

static int do_acl_dump(const char *path) {
  unsigned char buf[65536];
  ssize_t n = P_GET(path, ACL_NAME, buf, sizeof buf);
  ssize_t off;
  if (n < 0) {
    printf("%s\n", errno_name(errno));
    return 1;
  }
  if (n < 4 || get32(buf) != 2 || (n - 4) % 8 != 0) {
    printf("malformed\n");
    return 1;
  }
  for (off = 4; off < n; off += 8) {
    unsigned tag = get16(buf + off);
    unsigned perm = get16(buf + off + 2);
    unsigned long id = get32(buf + off + 4);
    char rwx[4];
    rwx[0] = (perm & 4) ? 'r' : '-';
    rwx[1] = (perm & 2) ? 'w' : '-';
    rwx[2] = (perm & 1) ? 'x' : '-';
    rwx[3] = '\0';
    switch (tag) {
    case TAG_USER_OBJ: printf("user::%s\n", rwx); break;
    case TAG_USER: printf("user:%lu:%s\n", id, rwx); break;
    case TAG_GROUP_OBJ: printf("group::%s\n", rwx); break;
    case TAG_GROUP: printf("group:%lu:%s\n", id, rwx); break;
    case TAG_MASK: printf("mask::%s\n", rwx); break;
    case TAG_OTHER: printf("other::%s\n", rwx); break;
    default: printf("tag-%#x:%s\n", tag, rwx); break;
    }
  }
  return 0;
}
#endif

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "";
  if (strcmp(mode, "set") == 0 && argc == 5) {
    return report(P_SET(argv[2], argv[3], argv[4], strlen(argv[4])));
  }
  if (strcmp(mode, "get") == 0 && argc == 4) {
    return do_get(argv[2], argv[3]);
  }
  if (strcmp(mode, "list") == 0 && argc == 4) {
    return do_list(argv[2], argv[3]);
  }
  if (strcmp(mode, "size") == 0 && argc == 5) {
    return do_size(argv[2], argv[3], argv[4]);
  }
  if (strcmp(mode, "namelen") == 0 && argc == 4) {
    return do_namelen(argv[2], argv[3]);
  }
#ifdef __linux__
  if (strcmp(mode, "acl-set") == 0 && argc == 8) {
    return do_acl_set(argv[2], argv + 3);
  }
  if (strcmp(mode, "acl-dump") == 0 && argc == 3) {
    return do_acl_dump(argv[2]);
  }
#endif
  fprintf(stderr, "usage: xattr_probe set|get|list|size|namelen|acl-set|acl-dump FILE ...\n");
  return 2;
}
