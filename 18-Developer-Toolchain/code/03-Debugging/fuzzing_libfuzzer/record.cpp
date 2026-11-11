// record.cpp - the code under test.
// The bug: the declared length is checked against the input size but not against
// out_cap, so a length above out_cap overflows the caller's buffer.
// Build with -DRECORD_FIXED to get the corrected bounds check.
#include "record.h"

#include <cstring>

namespace {
constexpr size_t kHeader = 5;
}  // namespace

int parse_record(const uint8_t *data, size_t size, uint8_t *out, size_t out_cap) {
    if (size < kHeader) return -1;
    // One comparison per byte, so each matched byte is a new edge for a coverage-guided fuzzer.
    if (data[0] != 'F') return -1;
    if (data[1] != 'U') return -1;
    if (data[2] != 'Z') return -1;
    if (data[3] != 'Z') return -1;
    const size_t len = data[4];
    if (len > size - kHeader) return -1;
#ifdef RECORD_FIXED
    if (len > out_cap) return -1;
#else
    (void)out_cap;
#endif
    std::memcpy(out, data + kHeader, len);
    return static_cast<int>(len);
}
