#include "market_pimpl.h"
#include <stdlib.h>

/* Internal struct definition completely isolated inside DSO */
struct MarketOrder {
    uint64_t order_id;
    double price;
    uint32_t quantity;
    uint32_t flags;
    uint64_t client_timestamp;
    char audit_trail[64];
};

MarketOrderHandle market_order_create(uint64_t id, double price, uint32_t qty) {
    MarketOrderHandle ord = (MarketOrderHandle)malloc(sizeof(struct MarketOrder));
    if (!ord) return NULL;
    ord->order_id = id;
    ord->price = price;
    ord->quantity = qty;
    ord->flags = 0x1;
    ord->client_timestamp = 1731650000ULL;
    return ord;
}

void market_order_destroy(MarketOrderHandle ord) {
    free(ord);
}

uint64_t market_order_get_id(MarketOrderHandle ord) {
    return ord ? ord->order_id : 0;
}

double market_order_get_price(MarketOrderHandle ord) {
    return ord ? ord->price : 0.0;
}

uint32_t market_order_get_quantity(MarketOrderHandle ord) {
    return ord ? ord->quantity : 0;
}

void market_order_set_price(MarketOrderHandle ord, double price) {
    if (ord) ord->price = price;
}

void market_order_process(MarketOrderHandle ord) {
    if (ord) ord->price *= 1.01;
}
