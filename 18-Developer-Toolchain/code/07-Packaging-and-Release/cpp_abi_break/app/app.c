#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <inttypes.h>
#include "market.h"

#define CANARY_VAL 0xCAFEBABE12345678ULL

struct BatchContext {
    struct Order items[2]; /* 2 * 24 = 48 bytes in v1 */
    uint64_t canary;       /* offset 48, size 8 */
};

int main(int argc, char** argv) {
    printf("[app runtime] sizeof(struct Order): %zu\n", sizeof(struct Order));

    /* Test 1: Stack layout & adjacent memory canary */
    struct BatchContext ctx;
    ctx.canary = CANARY_VAL;
    market_init_order(&ctx.items[0], 1001, 150.25, 10);
    market_init_order(&ctx.items[1], 1002, 250.50, 20);

    printf("[app runtime] items[0] id=%" PRIu64 " price=%.2f qty=%" PRIu32 "\n",
           ctx.items[0].order_id, ctx.items[0].price, ctx.items[0].quantity);
    printf("[app runtime] items[1] id=%" PRIu64 " price=%.2f qty=%" PRIu32 "\n",
           ctx.items[1].order_id, ctx.items[1].price, ctx.items[1].quantity);

    printf("[app runtime] Batch canary before process: %s\n",
           ctx.canary == CANARY_VAL ? "VALID" : "CORRUPTED");

    market_process_batch(ctx.items, 2);

    printf("[app runtime] items[0] price after process: %.2f\n", ctx.items[0].price);
    printf("[app runtime] items[1] price after process: %.2f\n", ctx.items[1].price);
    printf("[app runtime] items[1] qty after process:   %" PRIu32 "\n", ctx.items[1].quantity);
    printf("[app runtime] Batch canary after process:  %s\n",
           ctx.canary == CANARY_VAL ? "UNTOUCHED" : "CORRUPTED");

    /* Test 2: Heap buffer boundary verification */
    uint8_t* raw_buffer = (uint8_t*)malloc(sizeof(struct Order) + sizeof(uint64_t));
    if (!raw_buffer) return 1;

    struct Order* ord_ptr = (struct Order*)raw_buffer;
    uint64_t* heap_canary = (uint64_t*)(raw_buffer + sizeof(struct Order));
    *heap_canary = CANARY_VAL;

    market_init_order(ord_ptr, 5555, 99.95, 50);

    printf("[app runtime] Heap boundary canary after init: %s\n",
           *heap_canary == CANARY_VAL ? "UNTOUCHED" : "CORRUPTED");

    free(raw_buffer);
    return 0;
}
