#include <stdio.h>
#include <unistd.h>

int main(void) {
    printf("PPID_FAKE=%d\n", getppid() == 4242);
    return 0;
}
