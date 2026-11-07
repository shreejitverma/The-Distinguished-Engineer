#include <stdio.h>
#include <unistd.h>

int main(void) {
    /* Send query to stdout */
    printf("PING: req_1\n");

    /* Wait for response on stdin */
    char buf[128];
    if (fgets(buf, sizeof(buf), stdin) != NULL) {
        fprintf(stderr, "CLIENT_GOT: %s", buf);
        return 0;
    }
    return 1;
}
