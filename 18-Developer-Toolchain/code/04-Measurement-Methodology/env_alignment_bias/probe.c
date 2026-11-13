/*
 * probe.c - print where the kernel and the linker put things, as page and line offsets.
 *
 * Modes (argv[1]):
 *   strings  argv[0] offset within its 4 KiB page, and the offset predicted from the
 *            Linux exec layout: the string area ends 8 bytes below a page-aligned stack
 *            top and holds, from the top down, the AT_EXECFN string, the environment
 *            strings, and the argument strings.
 *   frame    offset within its 4 KiB page of a 16-byte-aligned 64-byte buffer in a callee
 *            frame (the address a benchmark's stack data would get).
 *   shift    the same callee buffer after the caller reserves k extra stack bytes with
 *            alloca, for k = 0, 8, 16, 24, 32, 48, 64, printed as the distance moved.
 *   code     offset of hot_loop() within its 64-byte cache line, and the signed distance
 *            from lab_pad (pad.c) to hot_loop, which shows the alignment padding.
 *
 * Only offsets are printed: a page-aligned base cancels out, so the output does not
 * depend on where ASLR put the page, only on the layout inside it.
 */
#include <alloca.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>

extern char **environ;
int hot_loop(const int *a, int n);
extern const char lab_pad[]; /* defined in pad.c; every build links it */

static uintptr_t page_offset(uintptr_t p) { return p & 4095u; }

__attribute__((noinline)) static uintptr_t frame_addr(void)
{
    _Alignas(16) volatile char buf[64];
    buf[0] = 1;
    buf[63] = 1;
    return (uintptr_t)buf;
}

__attribute__((noinline)) static uintptr_t shifted_frame(size_t k)
{
    volatile char *pad = alloca(k);
    if (k > 0) {
        pad[0] = 0;
    }
    return frame_addr();
}

static int mode_strings(int argc, char **argv)
{
    const char *execfn = (const char *)getauxval(AT_EXECFN);
    if (execfn == NULL) {
        fprintf(stderr, "probe: no AT_EXECFN in the auxiliary vector\n");
        return 1;
    }
    size_t below_top = sizeof(void *) + strlen(execfn) + 1;
    for (char **e = environ; *e != NULL; ++e) {
        below_top += strlen(*e) + 1;
    }
    for (int i = 0; i < argc; ++i) {
        below_top += strlen(argv[i]) + 1;
    }
    uintptr_t predicted = (uintptr_t)(4096u - (below_top % 4096u)) % 4096u;
    printf("%lu %lu\n", (unsigned long)page_offset((uintptr_t)argv[0]), (unsigned long)predicted);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: probe strings|frame|shift|code\n");
        return 2;
    }
    if (strcmp(argv[1], "strings") == 0) {
        return mode_strings(argc, argv);
    }
    if (strcmp(argv[1], "frame") == 0) {
        printf("%lu\n", (unsigned long)page_offset(frame_addr()));
        return 0;
    }
    if (strcmp(argv[1], "shift") == 0) {
        static const size_t ks[] = {0, 8, 16, 24, 32, 48, 64};
        uintptr_t base = shifted_frame(0);
        for (size_t i = 0; i < sizeof ks / sizeof ks[0]; ++i) {
            uintptr_t moved = base - shifted_frame(ks[i]);
            printf("%lu %lu\n", (unsigned long)ks[i], (unsigned long)moved);
        }
        return 0;
    }
    if (strcmp(argv[1], "code") == 0) {
        static const int data[4] = {1, 2, 3, 4};
        /* Call it so the linker keeps it and the result is observable. */
        if (hot_loop(data, 4) != 10) {
            fprintf(stderr, "probe: hot_loop returned a wrong sum\n");
            return 1;
        }
        long hot_minus_pad = (long)((intptr_t)&hot_loop - (intptr_t)lab_pad);
        printf("%lu %ld\n", (unsigned long)((uintptr_t)&hot_loop & 63u), hot_minus_pad);
        return 0;
    }
    fprintf(stderr, "probe: unknown mode %s\n", argv[1]);
    return 2;
}
