#ifndef MARKET_V2_BROKEN_H
#define MARKET_V2_BROKEN_H
#include <stdint.h>

struct Order {
    uint64_t id;               /* offset 0 */
    double price;              /* offset 8 */
    uint32_t quantity;         /* offset 16 */
    uint32_t flags;            /* offset 20 */
    uint64_t client_timestamp; /* offset 24 -> size 32 bytes */
};

void market_init_order(struct Order *order, uint64_t id, double price, uint32_t quantity);
int market_process_batch(struct Order *orders, int count);

#endif
