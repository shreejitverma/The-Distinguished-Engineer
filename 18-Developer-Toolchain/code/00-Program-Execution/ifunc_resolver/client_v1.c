#include <stdio.h>
#include <stdint.h>
#include <stddef.h>

uint32_t crypto_hash(const uint8_t *data, size_t len);
__asm__(".symver crypto_hash, crypto_hash@CRYPTO_1.0");

int main(void) {
    uint8_t buf[] = {10, 20, 30, 40};
    printf("client_v1 (CRYPTO_1.0) sum: %u\n", (unsigned int)crypto_hash(buf, sizeof(buf)));
    return 0;
}
