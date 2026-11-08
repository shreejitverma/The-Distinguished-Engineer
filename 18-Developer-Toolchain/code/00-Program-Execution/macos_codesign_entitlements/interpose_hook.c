#include <unistd.h>

static pid_t fake_getppid(void) {
    return 4242;
}

/* Same layout as DYLD_INTERPOSE in dyld's include/mach-o/dyld-interposing.h, declared by hand. */
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_getppid __attribute__((section("__DATA,__interpose"))) = {
    (const void *)fake_getppid, (const void *)getppid};
