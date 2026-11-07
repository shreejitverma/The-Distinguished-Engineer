#include "plugin.h"
#include "internal_math.h"
#include <stdio.h>

int plugin_run_task(const char *payload, uint32_t *out_hash) {
    if (!payload || !out_hash) return -1;
    printf("Plugin received task payload: '%s'\n", payload);
    uint32_t h = internal_calc_checksum(payload);
    *out_hash = h;
    const char *verdict = (h == EXPECTED_HASH) ? " (CORRECT)" : (h == INTERPOSED_HASH) ? " (CORRUPTED)" : " (UNEXPECTED)";
    printf("Internal hash computed: 0x%08x%s\n", h, verdict);
    return 0;
}
