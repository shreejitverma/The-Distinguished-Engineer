#include "internal_math.h"
#include <stddef.h>

/* FNV-1a 32-bit hash implementation */
uint32_t compute_hash(const char *key) {
    uint32_t h = 2166136261u;
    if (!key) return 0;
    while (*key) {
        h ^= (uint8_t)(*key++);
        h *= 16777619u;
    }
    return h;
}

uint32_t internal_calc_checksum(const char *payload) {
    return compute_hash(payload);
}
