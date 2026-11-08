#include <stdio.h>
#include <sys/ptrace.h>
#include <sys/types.h>
#include <unistd.h>

volatile const unsigned long long g_token = 0xDEADC0DECAFEFEEDULL;

int main(void) {
    ptrace(PT_DENY_ATTACH, 0, 0, 0);
    printf("READY %lx\n", (unsigned long)&g_token);
    fflush(stdout);
    pause();
    return 0;
}
