"""Minimal integer HdrHistogram, ported line by line from the Java reference.

Source: HdrHistogram/HdrHistogram tag HdrHistogram-2.2.2,
src/main/java/org/HdrHistogram/AbstractHistogram.java:
init(), getBucketsNeededToCoverValue(), getLengthForNumberOfBuckets(),
countsArrayIndex(), getBucketIndex(), getSubBucketIndex(), valueFromIndex(),
sizeOfEquivalentValueRange(), lowestEquivalentValue(), highestEquivalentValue(),
recordSingleValueWithExpectedInterval(), and getValueAtPercentile().

Only what the lab needs is ported: no auto-resize, no concurrency, no encoding.
"""

from __future__ import annotations

import math

LONG_MAX = (1 << 63) - 1


def _nlz64(x: int) -> int:
    """Long.numberOfLeadingZeros for a non-negative 64-bit value."""
    return 64 - x.bit_length()


class Histogram:
    def __init__(self, lowest_discernible: int, highest_trackable: int, significant_digits: int) -> None:
        if lowest_discernible < 1:
            raise ValueError("lowestDiscernibleValue must be >= 1")
        if highest_trackable < 2 * lowest_discernible:
            raise ValueError("highestTrackableValue must be >= 2 * lowestDiscernibleValue")
        if not 0 <= significant_digits <= 5:
            raise ValueError("numberOfSignificantValueDigits must be between 0 and 5")
        self.lowest_discernible = lowest_discernible
        self.highest_trackable = highest_trackable
        self.significant_digits = significant_digits

        largest_single_unit = 2 * 10**significant_digits
        # Java: (int) (Math.log(lowestDiscernibleValue) / Math.log(2)); exact for powers of two.
        self.unit_magnitude = lowest_discernible.bit_length() - 1
        # Java: (int) Math.ceil(Math.log(largest) / Math.log(2)); largest is never a power of two.
        sub_bucket_count_magnitude = (largest_single_unit - 1).bit_length()
        assert sub_bucket_count_magnitude == math.ceil(math.log2(largest_single_unit))
        self.sub_bucket_half_count_magnitude = sub_bucket_count_magnitude - 1
        self.sub_bucket_count = 1 << sub_bucket_count_magnitude
        self.sub_bucket_half_count = self.sub_bucket_count // 2
        self.sub_bucket_mask = (self.sub_bucket_count - 1) << self.unit_magnitude
        if sub_bucket_count_magnitude + self.unit_magnitude > 62:
            raise ValueError("cannot represent that many significant digits beyond lowestDiscernibleValue")
        self.leading_zero_count_base = 64 - self.unit_magnitude - sub_bucket_count_magnitude

        self.bucket_count = self._buckets_needed(highest_trackable)
        self.counts_length = (self.bucket_count + 1) * self.sub_bucket_half_count
        self.counts = [0] * self.counts_length
        self.total_count = 0
        self.max_value = 0

    def _buckets_needed(self, value: int) -> int:
        smallest_untrackable = self.sub_bucket_count << self.unit_magnitude
        buckets = 1
        while smallest_untrackable <= value:
            if smallest_untrackable > LONG_MAX // 2:
                return buckets + 1
            smallest_untrackable <<= 1
            buckets += 1
        return buckets

    # Indexing -------------------------------------------------------------

    def bucket_index(self, value: int) -> int:
        return self.leading_zero_count_base - _nlz64(value | self.sub_bucket_mask)

    def sub_bucket_index(self, value: int, bucket_index: int) -> int:
        return value >> (bucket_index + self.unit_magnitude)

    def counts_index(self, value: int) -> int:
        if value < 0:
            raise IndexError("Histogram recorded value cannot be negative.")
        b = self.bucket_index(value)
        s = self.sub_bucket_index(value, b)
        bucket_base_index = (b + 1) << self.sub_bucket_half_count_magnitude
        return bucket_base_index + (s - self.sub_bucket_half_count)

    def value_from_index(self, index: int) -> int:
        b = (index >> self.sub_bucket_half_count_magnitude) - 1
        s = (index & (self.sub_bucket_half_count - 1)) + self.sub_bucket_half_count
        if b < 0:
            s -= self.sub_bucket_half_count
            b = 0
        return s << (b + self.unit_magnitude)

    def size_of_equivalent_range(self, value: int) -> int:
        return 1 << (self.unit_magnitude + self.bucket_index(value))

    def lowest_equivalent(self, value: int) -> int:
        b = self.bucket_index(value)
        s = self.sub_bucket_index(value, b)
        return s << (b + self.unit_magnitude)

    def highest_equivalent(self, value: int) -> int:
        return self.lowest_equivalent(value) + self.size_of_equivalent_range(value) - 1

    # Recording ------------------------------------------------------------

    def record(self, value: int, count: int = 1) -> None:
        i = self.counts_index(value)
        if i >= self.counts_length:
            raise IndexError(f"value {value} outside of histogram covered range")
        self.counts[i] += count
        self.total_count += count
        self.max_value = max(self.max_value, value)

    def record_with_expected_interval(self, value: int, expected_interval: int) -> int:
        """recordSingleValueWithExpectedInterval(); returns how many synthetic values it added."""
        self.record(value)
        added = 0
        if expected_interval <= 0:
            return added
        missing = value - expected_interval
        while missing >= expected_interval:
            self.record(missing)
            added += 1
            missing -= expected_interval
        return added

    # Queries --------------------------------------------------------------

    def value_at_percentile(self, percentile: float) -> int:
        requested = min(max(math.nextafter(percentile, -math.inf), 0.0), 100.0)
        count_at = max(math.ceil((requested * self.total_count) / 100.0), 1)
        running = 0
        for i, c in enumerate(self.counts):
            running += c
            if running >= count_at:
                v = self.value_from_index(i)
                return self.lowest_equivalent(v) if percentile == 0.0 else self.highest_equivalent(v)
        return 0

    def max(self) -> int:
        return 0 if self.max_value == 0 else self.highest_equivalent(self.max_value)

    def footprint_estimate_bytes(self, word_size: int = 8) -> int:
        """README "Footprint estimation" formula for a long-count Histogram."""
        ratio = self.highest_trackable / self.sub_bucket_count
        log2_rounded_up = math.ceil(math.log2(ratio))
        return 512 + (word_size // 2) * (log2_rounded_up + 2) * self.sub_bucket_count
