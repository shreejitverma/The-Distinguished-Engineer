#ifndef ORDER_HPP
#define ORDER_HPP

#include <cstdint>

namespace order {
inline namespace v1 {

struct OrderRecord {
    uint32_t order_id;
    uint32_t symbol_id;
    double price;
    uint32_t quantity;
};

class OrderProcessor {
public:
    OrderProcessor();
    ~OrderProcessor();
    uint64_t process(const OrderRecord& rec);
    uint32_t version() const;
};

} // namespace v1
} // namespace order

#endif
