/*
 * showargv: print the argument vector exactly as the kernel delivered it.
 * Used as a "#!" interpreter, it shows what the kernel spliced in front of
 * the caller's arguments: argv[0], the optional shebang argument, the
 * script path.
 * Output: "argc=N", then one line per element, "[i]<bytes>", with every
 * byte outside printable ASCII written as a three-digit octal escape
 * (a carriage return prints as \015).
 * With the first argument "--sigpipe" it prints only "SIGPIPE=ignored" or
 * "SIGPIPE=default", the disposition it inherited.
 * Build: "${CC:-cc}" -o showargv showargv.c
 */
#include <signal.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
  if (argc > 1 && strcmp(argv[1], "--sigpipe") == 0) {
    struct sigaction sa;
    if (sigaction(SIGPIPE, NULL, &sa) != 0) {
      perror("sigaction");
      return 1;
    }
    puts(sa.sa_handler == SIG_IGN ? "SIGPIPE=ignored" : "SIGPIPE=default");
    return 0;
  }
  printf("argc=%d\n", argc);
  for (int i = 0; i < argc; i++) {
    printf("[%d]<", i);
    for (const unsigned char *p = (const unsigned char *)argv[i]; *p; p++) {
      if (*p >= 0x20 && *p < 0x7f) {
        putchar(*p);
      } else {
        printf("\\%03o", *p);
      }
    }
    puts(">");
  }
  return 0;
}
