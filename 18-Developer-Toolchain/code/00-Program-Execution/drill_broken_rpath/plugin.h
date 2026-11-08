#ifndef PLUGIN_H
#define PLUGIN_H

#include <stdint.h>

#ifdef EXPORT_API
  #define PLUGIN_API __attribute__((visibility("default")))
#else
  #define PLUGIN_API
#endif

/* FNV-1a 32-bit of "transaction_record_99", computed independently of the library under test. */
#define EXPECTED_HASH 0x043761a0u
/* The value the host's identically named compute_hash() returns when it preempts the library's. */
#define INTERPOSED_HASH 0x00001337u

PLUGIN_API int plugin_run_task(const char *payload, uint32_t *out_hash);

#endif /* PLUGIN_H */
