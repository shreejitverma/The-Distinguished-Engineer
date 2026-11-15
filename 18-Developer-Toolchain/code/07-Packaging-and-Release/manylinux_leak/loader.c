#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <dlfcn.h>

typedef uint32_t (*hash_fn)(const char *);

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <path_to_so>\n", argv[0]);
        return 2;
    }

    const char *so_path = argv[1];
    void *handle = dlopen(so_path, RTLD_NOW);
    if (!handle) {
        fprintf(stderr, "dlopen error: %s\n", dlerror());
        return 1;
    }

    dlerror(); /* Clear any existing error */
    hash_fn fn = (hash_fn)dlsym(handle, "compute_hash");
    char *err = dlerror();
    if (err != NULL) {
        fprintf(stderr, "dlsym error: %s\n", err);
        dlclose(handle);
        return 1;
    }

    uint32_t result = fn("transaction_record_99");
    printf("Result: SUCCESS! fast_hasher loaded and computed hash: 0x%08x\n", result);

    dlclose(handle);
    return 0;
}
