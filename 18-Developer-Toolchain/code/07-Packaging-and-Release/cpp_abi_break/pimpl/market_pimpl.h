#ifndef MARKET_PIMPL_H
#define MARKET_PIMPL_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque pointer handle: sizeof(MarketOrderHandle) is always sizeof(void*) = 8 bytes */
typedef struct MarketOrder* MarketOrderHandle;

MarketOrderHandle market_order_create(uint64_t id, double price, uint32_t qty);
void market_order_destroy(MarketOrderHandle ord);

uint64_t market_order_get_id(MarketOrderHandle ord);
double market_order_get_price(MarketOrderHandle ord);
uint32_t market_order_get_quantity(MarketOrderHandle ord);
void market_order_set_price(MarketOrderHandle ord, double price);
void market_order_process(MarketOrderHandle ord);

#ifdef __cplusplus
}
#endif

#endif /* MARKET_PIMPL_H */
