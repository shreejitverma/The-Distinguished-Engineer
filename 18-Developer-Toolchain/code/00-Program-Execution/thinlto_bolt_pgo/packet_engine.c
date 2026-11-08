#include <stdint.h>

__attribute__((noinline)) int handle_cold_error(int code) {
    volatile int dummy = code * 31 + 17;
    return dummy;
}

int packet_process(int type, int payload) {
    if (__builtin_expect(type == 1, 1)) {
        return payload * 7 + 13;
    } else {
        handle_cold_error(type);
        return -1;
    }
}

__attribute__((noinline)) int dead_debug_helper(int val) {
    return val * 1000 + 42;
}
