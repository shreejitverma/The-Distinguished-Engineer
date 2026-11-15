#include <stdio.h>
#include "mylib.h"

int main(void) {
#ifdef MYLIB_FEATURE
    printf("MYLIB_FEATURE reached main.c through declare_dependency: %d\n", MYLIB_FEATURE);
#else
    printf("MYLIB_FEATURE missing in main.c\n");
#endif
    printf("zlib version seen by mylib: %s\n", mylib_zlib_version());
    printf("deflate/inflate round trip: %s\n", mylib_roundtrip() ? "ok" : "FAILED");
    return 0;
}
