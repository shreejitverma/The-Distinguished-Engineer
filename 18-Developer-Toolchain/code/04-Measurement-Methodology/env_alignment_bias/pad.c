/*
 * pad.c - an unrelated object file with exactly 40 bytes of code (ten 4-byte words).
 * It stands in for "some other function that got a little bigger": it is never called,
 * but placing it before or after hot.o on the link line moves hot_loop.
 */
__asm__(".text\n"
        ".globl lab_pad\n"
        "lab_pad:\n"
        ".fill 10, 4, 0\n");
