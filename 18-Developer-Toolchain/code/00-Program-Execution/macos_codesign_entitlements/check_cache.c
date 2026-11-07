#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>

int main(void) {
    const char *path = "/usr/lib/libSystem.B.dylib";
    struct stat st;
    if (stat(path, &st) == 0) {
        printf("DYLD_SHARED_CACHE_DISK_STAT: EXISTS\n");
    } else {
        if (errno == ENOENT) {
            printf("DYLD_SHARED_CACHE_DISK_STAT: ENOENT\n");
        } else {
            printf("DYLD_SHARED_CACHE_DISK_STAT: ERRNO_%d\n", errno);
        }
    }

    void *handle = dlopen(path, RTLD_NOW);
    if (handle != NULL) {
        printf("DYLD_SHARED_CACHE_DLOPEN: SUCCESS\n");
        dlclose(handle);
    } else {
        printf("DYLD_SHARED_CACHE_DLOPEN: FAILED\n");
    }
    return 0;
}
