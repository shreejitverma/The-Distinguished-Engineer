#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
  if (argc < 2) {
    fprintf(stderr, "usage: probe_channel <mode> [args...]\n");
    return 1;
  }

  if (strcmp(argv[1], "argv_check") == 0) {
    if (argc < 3) {
      return 1;
    }
    const char *needle = argv[2];
    int found = 0;
    for (int i = 3; i < argc; i++) {
      if (strstr(argv[i], needle) != NULL) {
        found = 1;
        break;
      }
    }
    if (found) {
      printf("found in argv\n");
    } else {
      printf("not in argv\n");
    }
    return 0;
  }

  if (strcmp(argv[1], "env_check") == 0) {
    if (argc < 3) {
      return 1;
    }
    const char *val = getenv(argv[2]);
    if (val != NULL) {
      printf("env: %s\n", val);
    } else {
      printf("env: unset\n");
    }
    return 0;
  }

  if (strcmp(argv[1], "stdin_type") == 0) {
    struct stat st;
    if (fstat(0, &st) < 0) {
      perror("fstat");
      return 1;
    }
    if (S_ISREG(st.st_mode)) {
      printf("REGULAR_FILE size=%lld\n", (long long)st.st_size);
    } else if (S_ISFIFO(st.st_mode)) {
      printf("PIPE_OR_FIFO\n");
    } else {
      printf("OTHER mode=0%o\n", (unsigned int)st.st_mode);
    }
    return 0;
  }

  if (strcmp(argv[1], "stdin_read") == 0) {
    char buf[128];
    ssize_t n = read(0, buf, sizeof(buf) - 1);
    if (n < 0) {
      perror("read");
      return 1;
    }
    buf[n] = '\0';
    printf("stdin: %s\n", buf);
    return 0;
  }

  fprintf(stderr, "unknown mode: %s\n", argv[1]);
  return 1;
}
