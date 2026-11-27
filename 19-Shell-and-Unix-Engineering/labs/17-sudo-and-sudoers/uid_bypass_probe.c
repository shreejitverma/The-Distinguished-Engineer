/*
 * uid_bypass_probe.c: demonstrates why passing -1 (or 4294967295) to setreuid(2)
 * leaves real and effective user IDs unchanged without returning an error.
 * This POSIX.1-2024 credential semantics enabled CVE-2019-14287 in sudo.
 */

#include <stdio.h>
#include <unistd.h>
#include <sys/types.h>

int main(void) {
  uid_t ruid_before = getuid();
  uid_t euid_before = geteuid();

  /* POSIX.1-2024 XSH 3 setreuid: If ruid or euid is -1, the corresponding
   * ID of the current process shall be left unchanged.
   */
  if (setreuid((uid_t)-1, (uid_t)-1) != 0) {
    perror("setreuid");
    return 1;
  }

  uid_t ruid_after = getuid();
  uid_t euid_after = geteuid();

  if (ruid_before == ruid_after && euid_before == euid_after) {
    printf("SUCCESS: setreuid(-1, -1) left IDs unchanged (uid=%u euid=%u)\n",
           (unsigned int)ruid_after, (unsigned int)euid_after);
    return 0;
  }

  fprintf(stderr, "FAILURE: IDs changed unexpectedly\n");
  return 2;
}
