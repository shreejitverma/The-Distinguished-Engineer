"""A minimal port of HdrHistogram's recording and percentile paths.

Ported from org.HdrHistogram.AbstractHistogram at tag HdrHistogram-2.2.2:
init(), getBucketIndex(), getSubBucketIndex(), countsArrayIndex(), valueFromIndex(),
lowestEquivalentValue(), sizeOfEquivalentValueRange(), highestEquivalentValue(),
recordSingleValue(), recordSingleValueWithExpectedInterval(), getValueAtPercentile(),
and getMaxValue(). The method names below are the snake_case forms of those.
Auto-resize, value shifting, iterators, and the encoded formats are left out.
"""

from __future__ import annotations

import math


class HdrHistogram:
    def __init__(self, lowest_discernible: int, highest_trackable: int, significant_digits: int) -> None:
        if lowest_discernible < 1:
            raise ValueError("lowest_discernible must be at least 1")
        if highest_trackable < 2 * lowest_discernible:
            raise ValueError("highest_trackable must be at least 2 * lowest_discernible")
        if not 0 <= significant_digits <= 5:
            raise ValueError("significant_digits must be between 0 and 5")
        self.lowest_discernible = lowest_discernible
        self.highest_trackable = highest_trackable
        self.significant_digits = significant_digits

        # Unit resolution must hold up to 2 * 10^digits: +/- 1 at 1000 for 3 digits.
        largest_with_unit_resolution = 2 * 10**significant_digits
        # floor(log2(lowest)) and ceil(log2(largest)) in exact integer arithmetic.
        self.unit_magnitude = lowest_discernible.bit_length() - 1
        sub_bucket_count_magnitude = (largest_with_unit_resolution - 1).bit_length()
        self.sub_bucket_half_count_magnitude = sub_bucket_count_magnitude - 1
        self.sub_bucket_count = 1 << sub_bucket_count_magnitude
        self.sub_bucket_half_count = self.sub_bucket_count // 2
        self.sub_bucket_mask = (self.sub_bucket_count - 1) << self.unit_magnitude
        self.leading_zero_count_base = 64 - self.unit_magnitude - sub_bucket_count_magnitude

        self.bucket_count = self._buckets_needed(highest_trackable)
        self.counts = [0] * ((self.bucket_count + 1) * self.sub_bucket_half_count)
        self.total_count = 0
        self._max_value = 0

    def _buckets_needed(self, value: int) -> int:
        smallest_untrackable = self.sub_bucket_count << self.unit_magnitude
        buckets = 1
        while smallest_untrackable <= value:
            smallest_untrackable <<= 1
            buckets += 1
        return buckets

    def _bucket_index(self, value: int) -> int:
        leading_zeros = 64 - (value | self.sub_bucket_mask).bit_length()
        return self.leading_zero_count_base - leading_zeros

    def _sub_bucket_index(self, value: int, bucket_index: int) -> int:
        return value >> (bucket_index + self.unit_magnitude)

    def _counts_index(self, value: int) -> int:
        if value < 0:
            raise ValueError("histogram recorded value cannot be negative")
        bucket_index = self._bucket_index(value)
        sub_bucket_index = self._sub_bucket_index(value, bucket_index)
        bucket_base_index = (bucket_index + 1) << self.sub_bucket_half_count_magnitude
        return bucket_base_index + (sub_bucket_index - self.sub_bucket_half_count)

    def _value_from_index(self, index: int) -> int:
        bucket_index = (index >> self.sub_bucket_half_count_magnitude) - 1
        sub_bucket_index = (index & (self.sub_bucket_half_count - 1)) + self.sub_bucket_half_count
        if bucket_index < 0:
            sub_bucket_index -= self.sub_bucket_half_count
            bucket_index = 0
        return sub_bucket_index << (bucket_index + self.unit_magnitude)

    def lowest_equivalent_value(self, value: int) -> int:
        bucket_index = self._bucket_index(value)
        return self._sub_bucket_index(value, bucket_index) << (bucket_index + self.unit_magnitude)

    def size_of_equivalent_value_range(self, value: int) -> int:
        return 1 << (self.unit_magnitude + self._bucket_index(value))

    def highest_equivalent_value(self, value: int) -> int:
        return self.lowest_equivalent_value(value) + self.size_of_equivalent_value_range(value) - 1

    def record_value(self, value: int) -> None:
        index = self._counts_index(value)
        if index >= len(self.counts):
            raise ValueError(f"value {value} exceeds highest_trackable {self.highest_trackable}")
        self.counts[index] += 1
        self.total_count += 1
        if value > self._max_value:
            self._max_value = value

    def record_value_with_expected_interval(self, value: int, expected_interval: int) -> int:
        """Record `value`, then backfill the samples a blocked sender never took.

        Returns how many synthetic samples were added.
        """
        self.record_value(value)
        if expected_interval <= 0:
            return 0
        added = 0
        missing = value - expected_interval
        while missing >= expected_interval:
            self.record_value(missing)
            added += 1
            missing -= expected_interval
        return added

    def value_at_percentile(self, percentile: float) -> int:
        requested = min(max(math.nextafter(percentile, -math.inf), 0.0), 100.0)
        count_at_percentile = max(math.ceil(requested * self.total_count / 100.0), 1)
        running = 0
        for index, count in enumerate(self.counts):
            running += count
            if running >= count_at_percentile:
                value = self._value_from_index(index)
                if percentile == 0.0:
                    return self.lowest_equivalent_value(value)
                return self.highest_equivalent_value(value)
        return 0

    def max_value(self) -> int:
        return self.highest_equivalent_value(self._max_value) if self._max_value else 0
