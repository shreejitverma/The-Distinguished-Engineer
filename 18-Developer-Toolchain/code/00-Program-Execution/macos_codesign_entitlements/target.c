#include <stdio.h>
#include <unistd.h>

volatile const unsigned long long g_token = 0xDEADC0DECAFEFEEDULL;

int main(void) {
    printf("READY %lx\n", (unsigned long)&g_token);
    fflush(stdout);
    pause();
    return 0;
}
