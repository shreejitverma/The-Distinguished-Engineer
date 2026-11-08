#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>

/* usage: check_cache LABEL PATH - prints LABEL_STAT and LABEL_DLOPEN lines. */
int main(int argc, char **argv) {
    if (argc < 3) {
        return 1;
    }
    const char *label = argv[1];
    const char *path = argv[2];
    struct stat st;
    if (stat(path, &st) == 0) {
        printf("%s_STAT: EXISTS\n", label);
    } else if (errno == ENOENT) {
        printf("%s_STAT: ENOENT\n", label);
    } else {
        printf("%s_STAT: ERRNO_%d\n", label, errno);
    }

    void *handle = dlopen(path, RTLD_NOW);
    if (handle != NULL) {
        printf("%s_DLOPEN: SUCCESS\n", label);
        dlclose(handle);
    } else {
        printf("%s_DLOPEN: FAILED\n", label);
    }
    return 0;
}
