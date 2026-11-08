#include <unistd.h>

/* A plain same-named definition: no interpose tuple. */
pid_t getppid(void) {
    return 4242;
}
