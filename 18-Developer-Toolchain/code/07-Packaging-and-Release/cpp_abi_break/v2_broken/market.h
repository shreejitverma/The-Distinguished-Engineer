#ifndef MARKET_H
#define MARKET_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Market Order representation v1.1 (ABI Broken: added client_timestamp) */
struct Order {
    uint64_t order_id;         /* Offset 0, size 8 */
    double price;              /* Offset 8, size 8 */
    uint32_t quantity;         /* Offset 16, size 4 */
    uint32_t flags;            /* Offset 20, size 4 (fills old padding) */
    uint64_t client_timestamp; /* Offset 24, size 8 (GROWS STRUCT TO 32 BYTES) */
    /* Total sizeof(struct Order) = 32 */
};

void market_init_order(struct Order* ord, uint64_t id, double price, uint32_t qty);
void market_process_batch(struct Order* orders, size_t count);

#ifdef __cplusplus
}
#endif

#endif /* MARKET_H */
