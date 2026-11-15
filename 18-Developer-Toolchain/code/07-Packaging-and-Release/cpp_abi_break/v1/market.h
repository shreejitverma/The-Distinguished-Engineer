#ifndef MARKET_H
#define MARKET_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Market Order representation v1.0 */
struct Order {
    uint64_t order_id; /* Offset 0, size 8 */
    double price;      /* Offset 8, size 8 */
    uint32_t quantity; /* Offset 16, size 4 */
    /* 4 bytes implicit compiler padding on 64-bit platforms (alignof = 8) */
    /* Total sizeof(struct Order) = 24 */
};

void market_init_order(struct Order* ord, uint64_t id, double price, uint32_t qty);
void market_process_batch(struct Order* orders, size_t count);

#ifdef __cplusplus
}
#endif

#endif /* MARKET_H */
