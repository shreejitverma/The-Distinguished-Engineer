#include "order.hpp"

namespace order {
inline namespace v2 {

OrderProcessor::OrderProcessor() {}
OrderProcessor::~OrderProcessor() {}

uint64_t OrderProcessor::process(const OrderRecord& rec) {
    return (rec.client_id << 32) ^ (static_cast<uint64_t>(rec.order_id) * 100000ULL + rec.quantity);
}

uint32_t OrderProcessor::version() const {
    return 2;
}

} // namespace v2
} // namespace order
