#include "order.hpp"

namespace order {
inline namespace v1 {

OrderProcessor::OrderProcessor() {}
OrderProcessor::~OrderProcessor() {}

uint64_t OrderProcessor::process(const OrderRecord& rec) {
    return static_cast<uint64_t>(rec.order_id) * 100000ULL + static_cast<uint64_t>(rec.quantity);
}

uint32_t OrderProcessor::version() const {
    return 1;
}

} // namespace v1
} // namespace order
