#include <stdio.h>
#include <stdint.h>
#include <stddef.h>

uint64_t crypto_hash(const uint8_t *data, size_t len);

int main(void) {
    uint8_t buf[] = {10, 20, 30, 40};
    printf("client_v2 (CRYPTO_2.0) fnv: %016llx\n", (unsigned long long)crypto_hash(buf, sizeof(buf)));
    return 0;
}
