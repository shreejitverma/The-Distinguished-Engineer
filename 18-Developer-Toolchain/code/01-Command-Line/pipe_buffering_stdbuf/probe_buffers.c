#define _GNU_SOURCE
#include <stdio.h>
#include <stdio_ext.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <limits.h>

int main(void) {
    int p[2];
    if (pipe(p) != 0) {
        perror("pipe");
        return 1;
    }

    struct stat st;
    if (fstat(p[1], &st) != 0) {
        perror("fstat");
        return 1;
    }

    int default_cap = fcntl(p[1], F_GETPIPE_SZ);
    if (default_cap < 0) {
        perror("F_GETPIPE_SZ");
        return 1;
    }

    int new_cap = fcntl(p[1], F_SETPIPE_SZ, 1048576);
    if (new_cap < 0) {
        perror("F_SETPIPE_SZ");
        return 1;
    }

    /* glibc allocates the stdio buffer lazily, so write one byte before asking for its size. */
    FILE *fp = fdopen(p[1], "w");
    if (fp == NULL) {
        perror("fdopen");
        return 1;
    }
    fputc('x', fp);
    size_t stdio_buf = __fbufsize(fp);

    printf("1. Kernel pipe and libc buffer geometry:\n");
    printf("   pipe st_blksize:                    %ld B\n", (long)st.st_blksize);
    printf("   libc stdio buffer (__fbufsize):     %zu B\n", stdio_buf);
    printf("   POSIX atomic write limit (PIPE_BUF): %d B\n", PIPE_BUF);
    printf("   kernel pipe default capacity:       %d B (%d pages)\n", default_cap, default_cap / 4096);
    printf("   kernel pipe expanded capacity:      %d B (%d pages)\n", new_cap, new_cap / 4096);

    fclose(fp);
    close(p[0]);
    return 0;
}
