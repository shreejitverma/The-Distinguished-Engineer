#ifndef INTERNAL_MATH_H
#define INTERNAL_MATH_H

#include <stdint.h>

#ifdef EXPORT_API
  #define MATH_API __attribute__((visibility("default")))
#else
  #define MATH_API
#endif

/* Public API of internal_math: deliberately exported */
MATH_API uint32_t internal_calc_checksum(const char *payload);

/* Internal helper: without EXPORT_API, visibility is controlled by compiler default or attribute */
uint32_t compute_hash(const char *key);

#endif /* INTERNAL_MATH_H */
