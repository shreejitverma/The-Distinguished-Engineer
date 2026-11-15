#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "market_pimpl.h"

int main(void) {
    printf("[app_pimpl runtime] sizeof(MarketOrderHandle): %zu\n", sizeof(MarketOrderHandle));

    MarketOrderHandle ord = market_order_create(8888, 300.0, 15);
    if (!ord) return 1;

    printf("[app_pimpl runtime] Order ID: %llu, Price: %.2f, Qty: %u\n",
           (unsigned long long)market_order_get_id(ord),
           market_order_get_price(ord),
           (unsigned int)market_order_get_quantity(ord));

    market_order_process(ord);
    printf("[app_pimpl runtime] Processed Price: %.2f\n", market_order_get_price(ord));

    market_order_destroy(ord);
    printf("[app_pimpl runtime] PImpl lifecycle completed: PASS\n");
    return 0;
}
