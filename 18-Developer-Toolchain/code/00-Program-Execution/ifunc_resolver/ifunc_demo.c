#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#if defined(__aarch64__)
#include <asm/hwcap.h>
#include <sys/auxv.h>
#endif

static uint64_t hash_scalar(const uint8_t *data, size_t len) {
    uint64_t h = 0;
    for (size_t i = 0; i < len; ++i) {
        h = (h * 31) + data[i];
    }
    return h;
}

static uint64_t hash_vector(const uint8_t *data, size_t len) {
    uint64_t h = 0x12345678;
    for (size_t i = 0; i < len; ++i) {
        h = (h * 33) ^ data[i];
    }
    return h;
}

typedef uint64_t (*hash_fn)(const uint8_t *, size_t);

/*
 * The resolver runs once per process, during relocation processing, before main().
 * It must be self-contained: no PLT calls, no TLS, no stack protector, no sanitizer.
 * On AArch64 glibc passes AT_HWCAP (OR'd with bit 62) in x0 and a __ifunc_arg_t pointer in x1.
 * On x86-64 glibc passes nothing, and GCC requires __builtin_cpu_init() in a resolver.
 */
#if defined(__aarch64__)
static hash_fn resolve_hash(uint64_t hwcap, const void *ifunc_arg) {
    (void)ifunc_arg;
    return (hwcap & HWCAP_ASIMD) ? hash_vector : hash_scalar;
}
#elif defined(__x86_64__)
static hash_fn resolve_hash(void) {
    __builtin_cpu_init();
    return __builtin_cpu_supports("sse2") ? hash_vector : hash_scalar;
}
#else
#error "unsupported architecture"
#endif

uint64_t compute_digest(const uint8_t *data, size_t len) __attribute__((ifunc("resolve_hash")));

int main(void) {
    uint8_t buf[] = {1, 2, 3, 4};
    uint64_t got = compute_digest(buf, sizeof(buf));
    uint64_t vec = hash_vector(buf, sizeof(buf));
    uint64_t sca = hash_scalar(buf, sizeof(buf));
#if defined(__aarch64__)
    printf("AT_HWCAP has HWCAP_ASIMD: %s\n", (getauxval(AT_HWCAP) & HWCAP_ASIMD) ? "yes" : "no");
#endif
    printf("IFUNC selected: %s\n", got == vec ? "vector" : got == sca ? "scalar" : "unknown");
    printf("IFUNC digest: %016" PRIx64 "\n", got);
    printf("vector reference: %016" PRIx64 "\n", vec);
    printf("scalar reference: %016" PRIx64 "\n", sca);
    return 0;
}
