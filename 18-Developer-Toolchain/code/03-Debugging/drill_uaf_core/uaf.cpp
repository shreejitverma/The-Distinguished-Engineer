// A use-after-free that crashes far from the free, so the core's backtrace
// names the victim (the call through the freed object) and never the culprit
// (the free). The drill is to reconstruct the culprit from the heap bytes the
// core captured.
//
// Layout note: Order is 48 user bytes, so malloc rounds it to a 64-byte chunk
// (0x40) that lands in a per-thread tcache bin on glibc. When the chunk is
// freed, glibc's tcache_put overwrites its first 16 bytes with a tcache_entry:
// a safe-linked `next` pointer at offset 0 (over on_fill) and the process-wide
// `key` sentinel (glibc 2.34 and later) at offset 8 (over id). Because this chunk is the only one in
// its bin, next = PROTECT_PTR(&next, NULL) = (&next >> 12) ^ 0, a value that
// reveals back to NULL. Reading those two words out of the core is what proves
// the object was freed to the tcache and never reallocated.

#include <cstdint>
#include <cstdlib>
#include <cstring>

struct Order {
    void (*on_fill)(Order *);  // offset 0: tcache overwrites this with `next`
    uint64_t id;               // offset 8: tcache overwrites this with `key`
    uint64_t qty;              // offset 16: untouched by tcache
    char tag[24];              // offset 24
};

static void settle(Order *o) { o->qty = 0; }

__attribute__((noinline)) static Order *make_order(uint64_t id) {
    Order *o = static_cast<Order *>(std::malloc(sizeof(Order)));
    std::memset(o, 0, sizeof(Order));
    o->on_fill = settle;
    o->id = id;
    o->qty = 100;
    return o;
}

// The culprit. After this returns, the pointer the caller still holds dangles,
// and retire's frame is gone, so it cannot appear in the crash backtrace.
__attribute__((noinline)) static void retire(Order *o) { std::free(o); }

// The victim. The indirect call reads on_fill out of freed memory, which now
// holds the mangled tcache next pointer, and branches to a wild address.
__attribute__((noinline)) static void execute(Order *o) { o->on_fill(o); }

__attribute__((noinline)) static void dispatch(Order *o) { execute(o); }

int main() {
    Order *o = make_order(0x2222);
    retire(o);     // freed: sole chunk in its tcache bin, next mangles NULL
    dispatch(o);   // use-after-free through the dangling `o`
    return 0;
}
