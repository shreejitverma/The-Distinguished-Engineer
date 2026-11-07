#include <stdio.h>

void lib2_marker(void) {
    /* anchor symbol: libdep.so calls this so libdep2.so stays a DT_NEEDED dependency of libdep.so */
}

__attribute__((constructor(101)))
static void lib2_ctor_101(void) {
    printf("[dso2] constructor priority 101\n");
    fflush(stdout);
}

__attribute__((destructor(101)))
static void lib2_dtor_101(void) {
    printf("[dso2] destructor priority 101\n");
    fflush(stdout);
}
