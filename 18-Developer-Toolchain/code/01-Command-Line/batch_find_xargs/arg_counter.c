#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    FILE *f = fopen("batch_counts.log", "a");
    if (!f) {
        perror("fopen");
        return 1;
    }
    // Number of arguments passed to this execution (excluding argv[0])
    fprintf(f, "%d\n", argc - 1);
    fclose(f);
    return 0;
}
