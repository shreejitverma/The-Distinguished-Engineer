#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ptrace.h>
#include <sys/types.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        return 1;
    }
    int rc = ptrace(PT_ATTACH, atoi(argv[1]), 0, 0);
    printf("PT_ATTACH: rc=%d errno=%d\n", rc, rc == 0 ? 0 : errno);
    fflush(stdout);
    return 0;
}
