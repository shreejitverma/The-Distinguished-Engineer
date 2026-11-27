#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "market_v1.h"

struct ClientState {
    struct Order orders[2];
    uint64_t canary;
};

int main(void) {
    struct ClientState state;
    state.canary = 0xCAFEBABE12345678ULL;
    memset(state.orders, 0, sizeof(state.orders));

    printf("[DRILL 2] Client binary compiled against v1 headers (sizeof Order: %zu bytes)\n", sizeof(struct Order));
    market_init_order(&state.orders[0], 101, 450.50, 10);
    market_init_order(&state.orders[1], 102, 451.00, 20);

    /* Process batch using loaded DSO */
    market_process_batch(state.orders, 2);

    if (state.canary != 0xCAFEBABE12345678ULL) {
        printf("[DRILL 2] ABI_CORRUPTION_DETECTED: Canary corrupted! Expected 0xCAFEBABE12345678, found 0x%016llX\n", (unsigned long long)state.canary);
        return 1;
    }
    printf("[DRILL 2] SUCCESS: Canary intact (0x%016llX)\n", (unsigned long long)state.canary);
    return 0;
}
