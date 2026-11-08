#include <stdio.h>

__attribute__((constructor)) static void ctor(void) {
    printf("HOOK_EXECUTED\n");
}
