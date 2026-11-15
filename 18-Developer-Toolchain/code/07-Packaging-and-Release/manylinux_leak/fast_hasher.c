#include <stdint.h>
#include <stddef.h>
#include <string.h>

#if defined(PORTABLE)
/* Portable implementation avoiding GLIBC_2.38 strlcpy symbol leak */
static size_t portable_strlcpy(char *dst, const char *src, size_t siz) {
    size_t srclen = strlen(src);
    if (siz > 0) {
        size_t copylen = (srclen >= siz) ? (siz - 1) : srclen;
        memcpy(dst, src, copylen);
        dst[copylen] = '\0';
    }
    return srclen;
}
#define my_strlcpy portable_strlcpy
#else
/* Naively calls host libc strlcpy (introduced in glibc 2.38) */
#define my_strlcpy strlcpy
#endif

uint32_t compute_hash(const char *input) {
    char buf[64];
    my_strlcpy(buf, input, sizeof(buf));

    /* FNV-1a 32-bit hash */
    uint32_t hash = 2166136261u;
    for (size_t i = 0; buf[i] != '\0'; i++) {
        hash ^= (uint8_t)buf[i];
        hash *= 16777619u;
    }
    return hash;
}
