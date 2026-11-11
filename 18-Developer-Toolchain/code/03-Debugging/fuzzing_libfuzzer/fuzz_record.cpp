// fuzz_record.cpp - the libFuzzer target; AFL++ accepts the same entry point.
#include <cstddef>
#include <cstdint>
#include <memory>

#include "record.h"

namespace {
constexpr size_t kPayloadMax = 16;
}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    auto out = std::make_unique<uint8_t[]>(kPayloadMax);
    parse_record(data, size, out.get(), kPayloadMax);
    return 0;
}
