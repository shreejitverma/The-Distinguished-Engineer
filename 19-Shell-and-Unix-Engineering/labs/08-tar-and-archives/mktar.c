/* mktar: write a POSIX ustar archive to standard output from a list of entries, so a
 * lab can build archives that no well-behaved tar would create (members named "../x",
 * absolute paths, symlinks followed by files beneath them).
 *
 * Usage: mktar TYPE MODE NAME DATA [TYPE MODE NAME DATA]...
 *   TYPE  f (regular file, DATA is its contents), l (symlink, DATA is the target),
 *         d (directory, DATA is ignored; pass "-")
 *   MODE  octal permission bits, for example 0644
 *   NAME  member name; longer than 100 bytes is split into prefix and name at a slash
 *
 * Layout follows POSIX.1-2024 XCU pax, "ustar Interchange Format": 512-byte header,
 * data padded to 512 bytes, two zero blocks at the end, and the whole archive padded
 * to a 10240-byte record like the historical default blocking factor of 20.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { BLOCK = 512, RECORD = 10240 };

static unsigned long written;

static int put(const void *buf, size_t len) {
  if (fwrite(buf, 1, len, stdout) != len) {
    perror("mktar: write");
    return -1;
  }
  written += (unsigned long)len;
  return 0;
}

static int pad_to(unsigned long multiple) {
  static const char zero[BLOCK];
  while (written % multiple != 0) {
    unsigned long room = multiple - written % multiple;
    if (put(zero, room < BLOCK ? room : BLOCK) != 0) {
      return -1;
    }
  }
  return 0;
}

/* octal: write VALUE as LEN-1 zero-padded octal digits plus a NUL into FIELD. */
static void octal(char *field, size_t len, unsigned long value) {
  char tmp[32];
  snprintf(tmp, sizeof tmp, "%0*lo", (int)(len - 1), value);
  memcpy(field, tmp, len - 1);
  field[len - 1] = '\0';
}

/* split_name: store NAME in the name field, or in prefix + "/" + name when it is
 * longer than 100 bytes; return -1 when no slash gives a legal split. */
static int split_name(char *hdr, const char *name) {
  size_t n = strlen(name);
  size_t i;
  if (n <= 100) {
    memcpy(hdr, name, n);
    return 0;
  }
  for (i = n - 1; i > 0; i--) {
    if (name[i] == '/' && i <= 155 && n - i - 1 <= 100 && n - i - 1 > 0) {
      memcpy(hdr + 345, name, i);
      memcpy(hdr, name + i + 1, n - i - 1);
      return 0;
    }
  }
  fprintf(stderr, "mktar: name does not fit ustar: %s\n", name);
  return -1;
}

static int entry(const char *type, const char *mode, const char *name, const char *data) {
  char hdr[BLOCK];
  unsigned long sum = 0;
  size_t i, size = 0;

  memset(hdr, 0, sizeof hdr);
  if (split_name(hdr, name) != 0) {
    return -1;
  }
  octal(hdr + 100, 8, strtoul(mode, NULL, 8)); /* mode */
  octal(hdr + 108, 8, 1000);                   /* uid */
  octal(hdr + 116, 8, 1000);                   /* gid */
  octal(hdr + 136, 12, 1577836800UL);          /* mtime: 2020-01-01T00:00:00Z */
  switch (type[0]) {
    case 'f':
      hdr[156] = '0';
      size = strlen(data);
      break;
    case 'l':
      hdr[156] = '2';
      if (strlen(data) > 100) {
        fprintf(stderr, "mktar: symlink target longer than 100 bytes\n");
        return -1;
      }
      memcpy(hdr + 157, data, strlen(data));
      break;
    case 'd':
      hdr[156] = '5';
      break;
    default:
      fprintf(stderr, "mktar: unknown type %s\n", type);
      return -1;
  }
  octal(hdr + 124, 12, (unsigned long)size);
  memcpy(hdr + 257, "ustar", 6); /* magic "ustar" and a NUL */
  memcpy(hdr + 263, "00", 2);    /* version */
  memcpy(hdr + 265, "lab", 3);   /* uname */
  memcpy(hdr + 297, "lab", 3);   /* gname */
  memset(hdr + 148, ' ', 8);     /* chksum counts as eight spaces while summing */
  for (i = 0; i < sizeof hdr; i++) {
    sum += (unsigned char)hdr[i];
  }
  snprintf(hdr + 148, 8, "%06lo", sum); /* six digits, a NUL, then the space kept */
  hdr[155] = ' ';
  if (put(hdr, sizeof hdr) != 0) {
    return -1;
  }
  if (size > 0 && (put(data, size) != 0 || pad_to(BLOCK) != 0)) {
    return -1;
  }
  return 0;
}

int main(int argc, char **argv) {
  static const char zero[BLOCK];
  int i;
  if (argc < 5 || (argc - 1) % 4 != 0) {
    fprintf(stderr, "usage: mktar TYPE MODE NAME DATA [TYPE MODE NAME DATA]...\n");
    return 2;
  }
  for (i = 1; i + 3 < argc; i += 4) {
    if (entry(argv[i], argv[i + 1], argv[i + 2], argv[i + 3]) != 0) {
      return 1;
    }
  }
  if (put(zero, BLOCK) != 0 || put(zero, BLOCK) != 0 || pad_to(RECORD) != 0) {
    return 1;
  }
  if (fflush(stdout) != 0) {
    perror("mktar: flush");
    return 1;
  }
  return 0;
}
