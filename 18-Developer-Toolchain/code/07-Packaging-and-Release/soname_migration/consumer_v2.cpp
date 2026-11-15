#include "v2/order.hpp"
#include <iostream>

int main() {
    order::OrderRecord req{9999, 101, 42, 199.50, 500};
    order::OrderProcessor proc;
    std::cout << "consumer_v2: engine_version=" << proc.version()
              << " order_hash=" << proc.process(req) << "\n";
    return 0;
}
