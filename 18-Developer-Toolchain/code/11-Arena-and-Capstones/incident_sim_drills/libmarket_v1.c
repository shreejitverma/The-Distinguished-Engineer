#include "market_v1.h"

void market_init_order(struct Order *order, uint64_t id, double price, uint32_t quantity) {
    order->id = id;
    order->price = price;
    order->quantity = quantity;
    order->pad = 0;
}

int market_process_batch(struct Order *orders, int count) {
    int sum = 0;
    for (int i = 0; i < count; i++) {
        sum += (int)orders[i].quantity;
    }
    return sum;
}
