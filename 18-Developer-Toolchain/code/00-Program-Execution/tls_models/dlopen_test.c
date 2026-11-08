#include <stdio.h>
#include <dlfcn.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <lib_path>\n", argv[0]);
        return 2;
    }
    const char *lib_path = argv[1];
    void *h = dlopen(lib_path, RTLD_NOW);
    if (!h) {
        printf("dlopen %s: FAIL (%s)\n", lib_path, dlerror());
        return 1;
    }
    printf("dlopen %s: SUCCESS\n", lib_path);
    dlclose(h);
    return 0;
}
