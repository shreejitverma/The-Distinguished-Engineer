#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unwind.h>

/* The four lab functions; any other frame (libc start-up code) is not counted. */
static int is_lab_function(const char *name) {
    return strcmp(name, "leaf") == 0 || strcmp(name, "middle") == 0 ||
           strcmp(name, "outer") == 0 || strcmp(name, "main") == 0;
}

static _Unwind_Reason_Code cfi_cb(struct _Unwind_Context *ctx, void *arg) {
    int *count = (int *)arg;
    uintptr_t ip = _Unwind_GetIP(ctx);
    Dl_info info;
    if (dladdr((void *)ip, &info) && info.dli_sname && is_lab_function(info.dli_sname)) {
        printf("    [CFI] frame %d: %s\n", *count, info.dli_sname);
        (*count)++;
    }
    return _URC_NO_REASON;
}

static int trace_cfi(void) {
    int count = 0;
    _Unwind_Backtrace(cfi_cb, &count);
    return count;
}

/*
 * Walk the saved-frame-pointer chain. Two guards make the result independent of what the C library
 * leaves in the frame pointer register before main():
 *  - a hop larger than MAX_HOP bytes is rejected, because every lab frame is tiny;
 *  - the walk stops at the first return address that is not inside a lab function.
 */
#define MAX_HOP 4096u

static int trace_fp(void) {
    void **fp = (void **)__builtin_frame_address(0);
    int count = 0;
    while (fp && count < 16) {
        void **next_fp = (void **)fp[0];
        void *ret_addr = fp[1];
        Dl_info info;
        if (!ret_addr || !dladdr(ret_addr, &info) || !info.dli_sname || !is_lab_function(info.dli_sname)) {
            break;
        }
        printf("    [FP ] frame %d: %s\n", count, info.dli_sname);
        count++;
        if (next_fp <= fp || (uintptr_t)next_fp - (uintptr_t)fp > MAX_HOP ||
            ((uintptr_t)next_fp & (sizeof(void *) - 1))) {
            break;
        }
        fp = next_fp;
    }
    return count;
}

__attribute__((noinline)) int leaf(void) {
    printf("  Trace results from leaf():\n");
    printf("  -- DWARF CFI unwinding:\n");
    int cfi_count = trace_cfi();
    printf("  -- Frame Pointer unwinding:\n");
    int fp_count = trace_fp();
    printf("  Summary: CFI found %d frames, FP found %d frames\n", cfi_count, fp_count);
    return 1;
}

__attribute__((noinline)) int middle(void) {
    int x = leaf();
    __asm__ volatile("" : "+r"(x));
    return x + 1;
}

__attribute__((noinline)) int outer(void) {
    int x = middle();
    __asm__ volatile("" : "+r"(x));
    return x + 1;
}

int main(void) {
    int x = outer();
    __asm__ volatile("" : "+r"(x));
    return 0;
}
