#include "market.h"

void market_init_order(struct Order* ord, uint64_t id, double price, uint32_t qty) {
    if (!ord) return;
    ord->order_id = id;
    ord->price = price;
    ord->quantity = qty;
    ord->flags = 0x1;
    ord->client_timestamp = 1731650000ULL; /* Writes at offset 24 */
}

void market_process_batch(struct Order* orders, size_t count) {
    if (!orders) return;
    for (size_t i = 0; i < count; ++i) {
        orders[i].price *= 1.01;
        orders[i].flags = 0x2;
    }
}
