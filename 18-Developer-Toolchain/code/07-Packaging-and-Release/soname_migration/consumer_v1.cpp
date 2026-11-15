#include "v1/order.hpp"
#include <iostream>

int main() {
    order::OrderRecord req{101, 42, 199.50, 500};
    order::OrderProcessor proc;
    std::cout << "consumer_v1: engine_version=" << proc.version()
              << " order_hash=" << proc.process(req) << "\n";
    return 0;
}
