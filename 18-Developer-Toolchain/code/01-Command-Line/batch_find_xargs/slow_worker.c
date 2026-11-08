#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* Records "S" on start and "E" on exit in an O_APPEND log, so the line order is the real
 * event order. Each worker waits (up to 5 s) until `quorum` workers have started, so a pool
 * that really runs `quorum` workers at once always reaches a peak of `quorum`, and a pool
 * that serialises them never does. */
static int count_starts(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    int n = 0, c;
    while ((c = fgetc(f)) != EOF)
        if (c == 'S') n++;
    fclose(f);
    return n;
}

int main(int argc, char **argv) {
    if (argc < 2) return 1;
    int quorum = atoi(argv[1]);
    int fd = open("concurrency.log", O_WRONLY | O_APPEND | O_CREAT, 0644);
    if (fd < 0) return 1;
    if (write(fd, "S\n", 2) != 2) return 1;
    struct timespec ts = {0, 20 * 1000 * 1000};
    for (int i = 0; i < 250 && count_starts("concurrency.log") < quorum; i++)
        nanosleep(&ts, NULL);
    if (write(fd, "E\n", 2) != 2) return 1;
    close(fd);
    return 0;
}
