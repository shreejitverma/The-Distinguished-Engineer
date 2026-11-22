/* Load a libtoyz build, report the in-library effect counter right after dlopen and again after the IFUNC
 * symbol has been looked up and called.  Usage: driver /path/to/libtoyz.so */
#include <dlfcn.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    void *h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 2;
    }
    unsigned long (*effect)(void);
    unsigned int (*crc)(const unsigned char *, unsigned long);
    int (*selfcheck)(void);
    *(void **)(&effect) = dlsym(h, "toyz_effect");
    if (!effect) {
        fprintf(stderr, "missing toyz_effect\n");
        return 2;
    }
    printf("  toyz_effect() right after dlopen = %lu\n", effect());
    *(void **)(&crc) = dlsym(h, "toyz_crc");
    *(void **)(&selfcheck) = dlsym(h, "toyz_selfcheck");
    if (!crc || !selfcheck) {
        fprintf(stderr, "missing symbol\n");
        return 2;
    }
    printf("  toyz_crc(\"123456789\") = %08x (IEEE CRC-32 check value is cbf43926)\n",
           crc((const unsigned char *)"123456789", 9));
    printf("  toyz_selfcheck() = %d\n", selfcheck());
    printf("  toyz_effect() after first call = %lu\n", effect());
    dlclose(h);
    return 0;
}
