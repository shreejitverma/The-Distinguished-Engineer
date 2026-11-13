// workload.cpp - the code that PGO, AutoFDO, and BOLT optimize.
//
// It includes no headers on purpose, so the same file can also be compiled to an
// ELF object for any target (for example --target=x86_64-unknown-linux-gnu -c)
// to inspect section placement without a target sysroot.
//
// Shape of the workload, chosen so that each optimization has something to find:
// - 64 message handlers reached through a function-pointer table, where one type
//   (3) receives 90% of messages: indirect-call promotion has a dominant target.
// - Every handler starts with a rare validation failure (about 1 in 1024
//   messages) placed first in source order, so the static layout makes the
//   common path jump over the rare one: block placement has work to do.
// - dump_state() is never executed by the workload: a profile marks it cold.

using u32 = unsigned int;
using u64 = unsigned long long;

struct Msg {
    u32 type;
    u32 a;
    u64 b;
};

#define NOINLINE __attribute__((noinline))

extern "C" {

u64 g_bad = 0;

// Rare path: a deliberately bulky diagnostic hash (FNV-1a over 64 steps).
NOINLINE u64 report_bad(u32 type, const Msg* m) {
    u64 h = 1469598103934665603ull ^ type;
    for (u32 i = 0; i < 64; ++i) {
        h ^= static_cast<u64>(m->a + i);
        h *= 1099511628211ull;
    }
    ++g_bad;
    return h;
}

// Never executed by the workload; only main() under WORKLOAD_DUMP reaches it.
NOINLINE u64 dump_state(u64 x) {
    u64 h = x;
    for (u32 i = 0; i < 256; ++i) {
        h = (h ^ (h >> 31)) * 0x7fb5d329728ea185ull + i;
    }
    return h;
}

}  // extern "C"

#define HANDLER(N)                                                        \
    extern "C" NOINLINE u64 handle_##N(const Msg* m) {                    \
        if ((m->a & 0x3FFu) == 0x2Au) {                                   \
            return report_bad(N, m);                                      \
        }                                                                 \
        u64 x = m->b ^ (static_cast<u64>(N) + 1) * 0x9E3779B97F4A7C15ull; \
        for (u32 i = 0; i < (N % 4u) + 2u; ++i) {                         \
            x = ((x << 7) | (x >> 57)) + m->a + i;                        \
        }                                                                 \
        return x;                                                         \
    }

#define HANDLERS8(B)  \
    HANDLER(B##0)     \
    HANDLER(B##1)     \
    HANDLER(B##2)     \
    HANDLER(B##3)     \
    HANDLER(B##4)     \
    HANDLER(B##5)     \
    HANDLER(B##6)     \
    HANDLER(B##7)

// Handler numbers are written as two decimal digits that never start with 0,
// except the first row, which uses the single digits 0-7 (octal and decimal agree).
HANDLER(0)
HANDLER(1)
HANDLER(2)
HANDLER(3)
HANDLER(4)
HANDLER(5)
HANDLER(6)
HANDLER(7)
HANDLERS8(1)
HANDLERS8(2)
HANDLERS8(3)
HANDLERS8(4)
HANDLERS8(5)
HANDLERS8(6)
HANDLERS8(7)

using Handler = u64 (*)(const Msg*);

#define ROW(B) handle_##B##0, handle_##B##1, handle_##B##2, handle_##B##3, \
               handle_##B##4, handle_##B##5, handle_##B##6, handle_##B##7

// 64 entries: 0-7, then 10-17, 20-27, ..., 70-77.
static Handler const kTable[64] = {
    handle_0, handle_1, handle_2, handle_3, handle_4, handle_5, handle_6, handle_7,
    ROW(1), ROW(2), ROW(3), ROW(4), ROW(5), ROW(6), ROW(7),
};

static inline u64 xorshift64(u64& s) {
    s ^= s << 13;
    s ^= s >> 7;
    s ^= s << 17;
    return s;
}

extern "C" NOINLINE u64 run_batch(u64 seed, u64 n) {
    u64 s = seed;
    u64 acc = 0;
    for (u64 i = 0; i < n; ++i) {
        const u64 r = xorshift64(s);
        const u32 bucket = static_cast<u32>(r % 1000);
        u32 type;
        if (bucket < 900) {
            type = 3;                                        // 90%: one hot type
        } else if (bucket < 990) {
            type = static_cast<u32>(r >> 32) % 8u;           // 9%: types 0-7
        } else {
            type = static_cast<u32>(r >> 40) % 64u;          // 1%: any of 64
        }
        const Msg m{type, static_cast<u32>(r >> 16), r * 0x2545F4914F6CDD1Dull};
        acc += kTable[type](&m);
    }
    return acc;
}
