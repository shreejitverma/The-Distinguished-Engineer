#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#if defined(__GLIBC__)
#include <stdio_ext.h>
#endif
#include <unistd.h>

static void sleep_ms(long ms) {
    struct timespec ts = {.tv_sec = ms / 1000, .tv_nsec = (ms % 1000) * 1000000L};
    nanosleep(&ts, NULL);
}

// Reports the stdout buffering mode through public glibc APIs (stdio_ext.h), not private _flags bits.
// The first printf forces the buffer to be allocated, so __fbufsize reports the real size.
static void print_buffer_mode(void) {
    printf("STREAM_STDOUT_ISATTY: %d\n", isatty(fileno(stdout)));
#if defined(__GLIBC__)
    size_t size = __fbufsize(stdout);
    const char *mode = __flbf(stdout) ? "LINE_BUFFERED" : (size <= 1 ? "UNBUFFERED" : "BLOCK_BUFFERED");
    printf("STREAM_STDOUT_BUFFER_MODE: %s\n", mode);
    printf("STREAM_STDOUT_BUFFER_BYTES: %zu\n", size);
#else
    printf("STREAM_STDOUT_BUFFER_MODE: NON_GLIBC\n");
#endif
}

// Five records, one every 100 ms, so a reader can tell "arrives as written" from "arrives at exit".
static void emit_stream(void) {
    for (int i = 1; i <= 5; i++) {
        printf("[2026-11-07T08:00:0%dZ] GET path=/api/v1/users/profile_%d status=200 latency=10ms\n", i, i);
        sleep_ms(100);
    }
}

static void emit_burst(void) {
    // 5 normal records
    for (int i = 1; i <= 5; i++) {
        printf("[2026-11-07T08:00:0%dZ] GET path=/api/v1/users/profile_%d status=200 latency=10ms\n", i, i);
    }
    // 1 pathological record (nested quantifier target without slashes, terminated by !)
    printf("[2026-11-07T08:00:06Z] GET path=/api/v1/auth_service_v2_internal_token_validation_dump! status=403 latency=99ms\n");
    // 5 subsequent normal records
    for (int i = 7; i <= 11; i++) {
        printf("[2026-11-07T08:00:%02dZ] GET path=/api/v1/billing/invoice_%d status=200 latency=15ms\n", i, i);
    }
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        emit_burst();
        return 0;
    }

    if (strcmp(argv[1], "--mode=check") == 0) {
        print_buffer_mode();
    } else if (strcmp(argv[1], "--mode=stream") == 0) {
        emit_stream();
    } else if (strcmp(argv[1], "--mode=burst") == 0) {
        emit_burst();
    } else {
        fprintf(stderr, "Unknown mode: %s\n", argv[1]);
        return 1;
    }

    return 0;
}
