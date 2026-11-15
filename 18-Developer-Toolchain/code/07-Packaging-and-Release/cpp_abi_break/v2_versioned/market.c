#include <stdint.h>
#include <stddef.h>

/* Legacy v1 structure layout: 24 bytes */
struct Order_v1 {
    uint64_t order_id;
    double price;
    uint32_t quantity;
};

/* Modern v2 structure layout: 32 bytes */
struct Order_v2 {
    uint64_t order_id;
    double price;
    uint32_t quantity;
    uint32_t flags;
    uint64_t client_timestamp;
};

/* V1 implementation: respects 24-byte boundary */
void market_init_order_v1(struct Order_v1* ord, uint64_t id, double price, uint32_t qty) {
    if (!ord) return;
    ord->order_id = id;
    ord->price = price;
    ord->quantity = qty;
}

void market_process_batch_v1(struct Order_v1* orders, size_t count) {
    if (!orders) return;
    for (size_t i = 0; i < count; ++i) {
        orders[i].price *= 1.01;
    }
}

/* V2 implementation: handles 32-byte layout */
void market_init_order_v2(struct Order_v2* ord, uint64_t id, double price, uint32_t qty) {
    if (!ord) return;
    ord->order_id = id;
    ord->price = price;
    ord->quantity = qty;
    ord->flags = 0x1;
    ord->client_timestamp = 1731650000ULL;
}

void market_process_batch_v2(struct Order_v2* orders, size_t count) {
    if (!orders) return;
    for (size_t i = 0; i < count; ++i) {
        orders[i].price *= 1.01;
        orders[i].flags = 0x2;
    }
}

/* GNU symbol versioning directives */
__asm__(".symver market_init_order_v1, market_init_order@MARKET_1.0");
__asm__(".symver market_init_order_v2, market_init_order@@MARKET_2.0");

__asm__(".symver market_process_batch_v1, market_process_batch@MARKET_1.0");
__asm__(".symver market_process_batch_v2, market_process_batch@@MARKET_2.0");
