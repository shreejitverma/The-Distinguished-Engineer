#include <stdio.h>
#include <unistd.h>

int main(void) {
    char buf[128];
    if (fgets(buf, sizeof(buf), stdin) != NULL) {
        printf("PONG: ack_1\n");
        fflush(stdout);
        return 0;
    }
    return 1;
}
