#include <stdio.h>

int get_engine_version(void);

int main(void) {
    int v = get_engine_version();
    printf("[DRILL 5] Engine Version Resolved: %d\n", v);
    if (v == 201801) {
        printf("[DRILL 5] INTERPOSITION_DETECTED: System library hijacked resolution (201801)!\n");
        return 1;
    }
    printf("[DRILL 5] SUCCESS: Hermetic library resolved correctly (202611).\n");
    return 0;
}
