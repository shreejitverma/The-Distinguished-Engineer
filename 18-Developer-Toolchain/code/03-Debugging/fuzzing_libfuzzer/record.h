// record.h - a tiny length-prefixed record parser used by every part of the lab.
// Wire format: "FUZZ" magic, one length byte N, then N payload bytes.
#pragma once
#include <cstddef>
#include <cstdint>

// Copies the payload into out[0, out_cap) and returns its length, or -1 when the record is malformed.
int parse_record(const uint8_t *data, size_t size, uint8_t *out, size_t out_cap);
