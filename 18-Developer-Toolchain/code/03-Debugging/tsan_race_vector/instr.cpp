// Compiled to assembly only, to list the runtime calls the TSan pass inserts per function.
#include <atomic>

extern "C" {
int shared_word;
const int table[4] = {1, 2, 3, 4};
std::atomic<int> flag{0};
void opaque(int *p);

// Plain store to a global: one __tsan_write4.
void plain_write(int v) { shared_word = v; }

// Read-modify-write of a global: the read before the write is folded into the write.
void plain_increment() { ++shared_word; }

// Load from constant data cannot race, so it is not instrumented.
int const_read(int i) { return table[i & 3]; }

// A local whose address escapes is shared memory as far as the pass knows.
int escaped_local() {
    int x = 1;
    opaque(&x);
    return x;
}

// Atomics become calls that carry the memory order.
void release_store() { flag.store(1, std::memory_order_release); }
int acquire_load() { return flag.load(std::memory_order_acquire); }

// A fence becomes a call too; the runtime only issues a hardware barrier.
void release_fence() { std::atomic_thread_fence(std::memory_order_release); }
}
