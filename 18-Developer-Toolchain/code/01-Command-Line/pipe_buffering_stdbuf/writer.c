#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* Usage: writer [phase1_lines]; 70 lines (4130 bytes) in total, split at phase1_lines (default 30). */
int main(int argc, char **argv) {
    int split = argc > 1 ? atoi(argv[1]) : 30;

    /* Phase 1: Write `split` lines of 59 bytes each (30 lines = 1770 bytes by default) */
    for (int i = 0; i < split; i++) {
        printf("Record %02d: -----------------------------------------------\n", i);
    }
    /* Signal Phase 1 completion over unbuffered stderr */
    fprintf(stderr, "PHASE1_WRITTEN\n");

    /* Wait for acknowledgement token from reader on stdin */
    char ack = 0;
    if (read(0, &ack, 1) <= 0) {
        return 1;
    }

    /* Phase 2: Write the remaining lines (total 4130 bytes > 4096) */
    for (int i = split; i < 70; i++) {
        printf("Record %02d: -----------------------------------------------\n", i);
    }
    fprintf(stderr, "PHASE2_WRITTEN\n");
    return 0;
}
