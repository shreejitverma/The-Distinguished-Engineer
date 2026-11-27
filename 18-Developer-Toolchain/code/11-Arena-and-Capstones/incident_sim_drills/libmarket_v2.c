#include "market_v2_broken.h"

void market_init_order(struct Order *order, uint64_t id, double price, uint32_t quantity) {
    order->id = id;
    order->price = price;
    order->quantity = quantity;
    order->flags = 1;
    order->client_timestamp = 0xAA55AA55AA55AA55ULL;
}

int market_process_batch(struct Order *orders, int count) {
    int sum = 0;
    for (int i = 0; i < count; i++) {
        orders[i].client_timestamp = 0xDEADBEEFCAFE0000ULL + i;
        sum += (int)orders[i].quantity;
    }
    return sum;
}
