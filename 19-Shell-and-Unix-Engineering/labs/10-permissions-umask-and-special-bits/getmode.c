#include <stdio.h>
#include <sys/stat.h>

int main(int argc, char **argv) {
  struct stat st;
  int i;

  for (i = 1; i < argc; i++) {
    if (stat(argv[i], &st) == 0) {
      printf("%04o\n", (unsigned int)(st.st_mode & 07777));
    } else {
      perror(argv[i]);
      return 1;
    }
  }
  return 0;
}
