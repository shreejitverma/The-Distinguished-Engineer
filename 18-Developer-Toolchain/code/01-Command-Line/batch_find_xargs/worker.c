#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc < 2) return 1;

    if (strcmp(argv[1], "abort") == 0) {
        return 255;
    }
    if (strcmp(argv[1], "fail") == 0) {
        return 42;
    }

    FILE *f = fopen("worker_processed.log", "a");
    if (!f) return 1;
    if (strncmp(argv[1], "task", 4) == 0) {
        fprintf(f, "%s\n", argv[1]);
    } else {
        fprintf(f, "RECORD\n");
    }
    fclose(f);
    return 0;
}
