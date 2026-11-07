#include <stdint.h>
#include <stddef.h>

uint32_t crypto_hash_v1(const uint8_t *data, size_t len) {
    uint32_t sum = 0;
    for (size_t i = 0; i < len; ++i) {
        sum += data[i];
    }
    return sum;
}
__asm__(".symver crypto_hash_v1, crypto_hash@CRYPTO_1.0");

uint64_t crypto_hash_v2(const uint8_t *data, size_t len) {
    uint64_t hash = 0xcbf29ce484222325ULL;
    for (size_t i = 0; i < len; ++i) {
        hash ^= data[i];
        hash *= 0x100000001b3ULL;
    }
    return hash;
}
__asm__(".symver crypto_hash_v2, crypto_hash@@CRYPTO_2.0");
