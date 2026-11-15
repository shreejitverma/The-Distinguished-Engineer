/* showargv.c - print every argv element as "argv[N]=<VALUE>", one per line.
 * Used as the #! interpreter of a script, it shows exactly the argument vector the
 * kernel built: the interpreter path, the #! argument(s), the script pathname, and
 * the caller's arguments; argv[0] passed to execve() is gone.
 */
#include <stdio.h>

int main(int argc, char **argv)
{
  for (int i = 0; i < argc; i++) {
    printf("argv[%d]=<%s>\n", i, argv[i]);
  }
  return 0;
}
