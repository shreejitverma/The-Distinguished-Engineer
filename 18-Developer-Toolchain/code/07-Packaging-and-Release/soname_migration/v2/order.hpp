#ifndef ORDER_HPP
#define ORDER_HPP

#include <cstdint>

namespace order {
inline namespace v2 {

// ABI Break: 64-bit client_id added, altering memory layout and struct size.
struct OrderRecord {
    uint64_t client_id;
    uint32_t order_id;
    uint32_t symbol_id;
    double price;
    uint64_t quantity;
};

class OrderProcessor {
public:
    OrderProcessor();
    ~OrderProcessor();
    uint64_t process(const OrderRecord& rec);
    uint32_t version() const;
};

} // namespace v2
} // namespace order

#endif
