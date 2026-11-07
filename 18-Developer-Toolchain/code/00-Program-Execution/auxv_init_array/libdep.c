#include <stdio.h>

extern void lib2_marker(void); /* from libdep2.so, a dependency of this library */

void lib_marker(void) {
    /* anchor symbol to force the exe -> libdep.so dependency; the call keeps libdep2.so needed */
    lib2_marker();
}

__attribute__((constructor(101)))
static void lib_ctor_101(void) {
    printf("[dso] constructor priority 101\n");
    fflush(stdout);
}

__attribute__((constructor(200)))
static void lib_ctor_200(void) {
    printf("[dso] constructor priority 200\n");
    fflush(stdout);
}

__attribute__((constructor))
static void lib_ctor_unnumbered(void) {
    printf("[dso] constructor unnumbered\n");
    fflush(stdout);
}

__attribute__((constructor(65535)))
static void lib_ctor_65535(void) {
    printf("[dso] constructor priority 65535\n");
    fflush(stdout);
}

__attribute__((destructor(101)))
static void lib_dtor_101(void) {
    printf("[dso] destructor priority 101\n");
    fflush(stdout);
}

__attribute__((destructor(200)))
static void lib_dtor_200(void) {
    printf("[dso] destructor priority 200\n");
    fflush(stdout);
}

__attribute__((destructor))
static void lib_dtor_unnumbered(void) {
    printf("[dso] destructor unnumbered\n");
    fflush(stdout);
}

__attribute__((destructor(65535)))
static void lib_dtor_65535(void) {
    printf("[dso] destructor priority 65535\n");
    fflush(stdout);
}
