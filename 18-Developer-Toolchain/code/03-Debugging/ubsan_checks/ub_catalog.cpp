// One undefined-behavior (or suspicious-but-defined) operation per case.
// Every case sits in its own noinline function so each check has its own
// source location: UBSan reports a given location only once per process.
// Values come through opaque() so the compiler cannot fold the UB away.
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

template <typename T>
__attribute__((noinline)) T opaque(T v) {
    volatile T x = v;
    return x;
}

__attribute__((noinline)) long signed_add() {
    int k = opaque(INT_MAX);
    return k + opaque(1);
}

__attribute__((noinline)) long int_min_div() {
    return opaque(INT_MIN) / opaque(-1);
}

__attribute__((noinline)) long div_zero() {
    return opaque(7) / opaque(0);
}

__attribute__((noinline)) long shift_exponent() {
    return 1 << opaque(32);
}

__attribute__((noinline)) long shift_negative_base() {
    return opaque(-1) << 1;
}

__attribute__((noinline)) long shift_overflow_base() {
    return opaque(3) << 31;
}

__attribute__((noinline)) long array_bounds() {
    int arr[4] = {10, 20, 30, 40};
    volatile int v = arr[opaque(4)];  // the value read is stack garbage
    (void)v;
    return -1;
}

__attribute__((noinline)) long misaligned_load() {
    alignas(8) unsigned char buf[16] = {};
    int* p = reinterpret_cast<int*>(buf + opaque(1));
    return *p;
}

__attribute__((noinline)) long null_reference() {
    int* p = opaque<int*>(nullptr);
    int& r = *p;  // binding alone is UB; r is never read
    return &r == nullptr;
}

__attribute__((noinline)) long null_plus_offset() {
    char* p = opaque<char*>(nullptr);
    char* q = p + opaque(16);
    return q != nullptr;
}

__attribute__((noinline)) long bool_load() {
    unsigned char raw = opaque<unsigned char>(2);
    bool b;
    std::memcpy(&b, &raw, 1);
    return b ? 1 : 0;
}

enum Color { Red, Green, Blue };  // no fixed underlying type: value range is 0..3

__attribute__((noinline)) long enum_load(unsigned v) {
    Color c;
    std::memcpy(&c, &v, sizeof v);
    return static_cast<long>(c);
}

__attribute__((noinline)) long enum_in_range() { return enum_load(opaque(3u)); }
__attribute__((noinline)) long enum_out_of_range() { return enum_load(opaque(4u)); }

enum class Side : int { Buy, Sell };  // fixed underlying type: every int is a value

__attribute__((noinline)) long enum_class_unlisted() {
    int v = opaque(7);
    Side s;
    std::memcpy(&s, &v, sizeof v);
    return static_cast<long>(s);
}

__attribute__((noinline)) long float_cast() {
    double d = opaque(1e10);
    return static_cast<int>(d);
}

__attribute__((noinline)) long builtin_ctz() {
    return __builtin_ctz(opaque(0u));
}

__attribute__((noinline, nonnull(1))) long deref_nonnull(const int* p) {
    return p == nullptr ? -1 : *p;
}

__attribute__((noinline)) long nonnull_arg() {
    return deref_nonnull(opaque<const int*>(nullptr));
}

__attribute__((noinline)) long unsigned_wrap() {
    unsigned u = opaque(0u);
    return u - 1u;  // defined: wraps to UINT_MAX
}

__attribute__((noinline)) long implicit_truncation() {
    int big = opaque(300);
    unsigned char c = big;  // defined: 300 mod 256 == 44
    return c;
}

struct Animal {
    virtual ~Animal() = default;
    virtual int legs() const { return 0; }
};
struct Dog : Animal {
    int legs() const override { return 4; }
    int bark() const { return 1; }
};
struct Cat : Animal {
    int legs() const override { return 4; }
};

__attribute__((noinline)) long bad_downcast() {
    Animal* a = opaque<Animal*>(new Cat);
    long r = static_cast<Dog*>(a)->bark();
    delete a;
    return r;
}

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wreturn-type"
__attribute__((noinline)) int falls_off(int x) {
    if (x > 0) return 1;
}
#pragma clang diagnostic pop

__attribute__((noinline)) long missing_return() {
    return falls_off(opaque(0));
}

__attribute__((noinline)) long same_site_loop() {
    long sum = 0;
    for (int i = 0; i < 3; ++i) {
        int k = opaque(INT_MAX - i);
        sum += k + opaque(1 + i);  // one source location, three overflows
    }
    return sum;
}

struct Case {
    const char* name;
    long (*fn)();
};

const Case kCases[] = {
    {"signed-add", signed_add},
    {"int-min-div", int_min_div},
    {"div-zero", div_zero},
    {"shift-exponent", shift_exponent},
    {"shift-negative-base", shift_negative_base},
    {"shift-overflow-base", shift_overflow_base},
    {"array-bounds", array_bounds},
    {"misaligned-load", misaligned_load},
    {"null-reference", null_reference},
    {"null-plus-offset", null_plus_offset},
    {"bool-load", bool_load},
    {"enum-in-range", enum_in_range},
    {"enum-out-of-range", enum_out_of_range},
    {"enum-class-unlisted", enum_class_unlisted},
    {"float-cast", float_cast},
    {"builtin-ctz", builtin_ctz},
    {"nonnull-arg", nonnull_arg},
    {"unsigned-wrap", unsigned_wrap},
    {"implicit-truncation", implicit_truncation},
    {"bad-downcast", bad_downcast},
    {"missing-return", missing_return},
    {"same-site-loop", same_site_loop},
};

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--list") == 0) {
        for (const Case& c : kCases) std::printf("%s\n", c.name);
        return 0;
    }
    for (int i = 1; i < argc; ++i) {
        bool found = false;
        for (const Case& c : kCases) {
            if (std::strcmp(argv[i], c.name) == 0) {
                found = true;
                long r = c.fn();
                std::printf("%s returned %ld\n", c.name, r);
                std::fflush(stdout);
            }
        }
        if (!found) {
            std::fprintf(stderr, "unknown case %s\n", argv[i]);
            return 2;
        }
    }
    std::printf("main reached the end\n");
    return 0;
}
