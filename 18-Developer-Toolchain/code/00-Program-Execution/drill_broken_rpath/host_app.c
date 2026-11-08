#include <stdio.h>
#include <stdint.h>
#include "plugin.h"

/* Host defines an identically-named internal function with default visibility */
uint32_t compute_hash(const char *key) {
    (void)key;
    return INTERPOSED_HASH;
}

int main(void) {
    printf("Host started: dispatching task to plugin...\n");
    const char *workload = "transaction_record_99";
    uint32_t result_hash = 0;

    if (plugin_run_task(workload, &result_hash) != 0) {
        fprintf(stderr, "Plugin execution failed\n");
        return 1;
    }

    /* Exit status: 0 only for the expected hash, 3 for the interposed value, 4 for anything else. */
    if (result_hash == EXPECTED_HASH) {
        printf("Result: SUCCESS! Computation matches expected hash.\n");
        return 0;
    }
    if (result_hash == INTERPOSED_HASH) {
        printf("Result: MISMATCH! Expected real hash, got host interposed 0x%08x\n", INTERPOSED_HASH);
        return 3;
    }
    printf("Result: UNEXPECTED hash 0x%08x\n", result_hash);
    return 4;
}
