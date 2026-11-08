// AddressSanitizer shadow probe.
//
// Prints the shadow bytes the ASan runtime wrote for heap, stack, global, container, and
// user-poisoned memory, rebuilds the inline check the compiler emits and compares it with the
// runtime's own byte-exact answer, and measures whether a freed chunk is handed out again.
// Output is derived facts only (shadow byte values, counts, yes/no), never raw addresses.

#include <sanitizer/asan_interface.h>

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

std::size_t g_scale = 0;
std::size_t g_offset = 0;

char g13[13];  // a 13-byte global: the compiler appends a redzone after it

// Reading shadow from instrumented code would itself be checked: the shadow of a shadow
// address lands in the protected shadow gap and faults, so the reader opts out of ASan.
__attribute__((no_sanitize("address"))) std::uint8_t shadow_of(std::uintptr_t a) {
  return *reinterpret_cast<const volatile std::uint8_t*>((a >> g_scale) + g_offset);
}

std::uintptr_t addr(const void* p) { return reinterpret_cast<std::uintptr_t>(p); }

void dump(const char* label, const void* base, long from, long to) {
  std::printf("  %-34s", label);
  for (long off = from; off < to; off += 8) {
    std::printf(" %02x", shadow_of(addr(base) + static_cast<std::uintptr_t>(off)));
  }
  std::printf("\n");
}

// The check clang and GCC inline before an aligned 1-, 2-, 4-, 8-, or 16-byte access.
// k == 0: all 8 bytes of the granule are addressable.
// 1..7: only the first k bytes are addressable.
// negative (0xf1..0xfe as int8_t): the whole granule is poisoned.
__attribute__((no_sanitize("address"))) bool toy_check_fails(std::uintptr_t a, std::size_t size) {
  if (size == 16) {
    // One 2-byte shadow load covers both granules; any nonzero byte is an error.
    return shadow_of(a) != 0 || shadow_of(a + 8) != 0;
  }
  const auto k = static_cast<std::int8_t>(shadow_of(a));
  if (k == 0) return false;
  if (size == 8) return true;  // a full-granule access has no slow path
  const auto last = static_cast<std::int8_t>((a & 7) + size - 1);
  return last >= k;  // signed compare: negative k always fails
}

struct Tally {
  int checked = 0;
  int agreed = 0;
};

// Compare the toy check with __asan_region_is_poisoned, which tests every byte, for every
// naturally aligned access of each size whose first byte lies in [base + from, base + to).
void compare(const char* base, long from, long to, Tally& t) {
  const std::size_t sizes[] = {1, 2, 4, 8, 16};
  for (std::size_t size : sizes) {
    for (long off = from; off < to; ++off) {
      const std::uintptr_t a = addr(base) + static_cast<std::uintptr_t>(off);
      if (a % size != 0) continue;
      const bool runtime_bad =
          __asan_region_is_poisoned(reinterpret_cast<void*>(a), size) != nullptr;
      ++t.checked;
      if (toy_check_fails(a, size) == runtime_bad) ++t.agreed;
    }
  }
}

__attribute__((noinline)) void stack_frame_dump() {
  char buf[10];
  volatile char* keep = buf;  // forces buf into an instrumented stack slot
  keep[0] = 1;
  dump("char buf[10] on the stack [-32,+32)", buf, -32, 32);
}

__attribute__((noinline)) void stack_frame_compare(Tally& t) {
  char buf[10];
  volatile char* keep = buf;
  keep[0] = 1;
  compare(buf, -32, 32, t);
}

}  // namespace

int main() {
  __asan_get_shadow_mapping(&g_scale, &g_offset);
  std::printf("== 1. Mapping: shadow = (addr >> scale) + offset\n");
  std::printf("  scale %zu, granule %zu bytes per shadow byte\n", g_scale,
              static_cast<std::size_t>(1) << g_scale);
  if (g_offset != 0 && (g_offset & (g_offset - 1)) == 0) {
    int log2 = 0;
    while ((static_cast<std::size_t>(1) << log2) != g_offset) ++log2;
    std::printf("  offset 2^%d (a compile-time constant on this target)\n", log2);
  } else {
    // Linux x86_64 uses the constant 0x7fff8000; macOS arm64 picks the offset at startup.
    std::printf("  offset is not a power of two\n");
  }

  std::printf("\n== 2. Partial granules: shadow of malloc(n), first three granules\n");
  const std::size_t ns[] = {1, 7, 8, 9, 13, 16, 17};
  for (std::size_t n : ns) {
    char* p = static_cast<char*>(std::malloc(n));
    char label[32];
    std::snprintf(label, sizeof label, "malloc(%zu)", n);
    dump(label, p, 0, 24);
    std::free(p);
  }

  std::printf("\n== 3. Shadow around objects, one byte per 8-byte granule\n");
  char* h = static_cast<char*>(std::malloc(13));
  dump("malloc(13) [-16,+32)", h, -16, 32);
  stack_frame_dump();
  dump("global char g13[13] [0,+32)", g13, 0, 32);
  std::vector<char> v;
  v.reserve(32);
  v.resize(5);
  dump("vector<char> size 5 cap 32 [0,+32)", v.data(), 0, 32);
  char* r = static_cast<char*>(std::malloc(64));
  __asan_poison_memory_region(r + 16, 16);
  dump("poison [16,32) of malloc(64)", r, 0, 64);
  __asan_unpoison_memory_region(r, 64);
  __asan_poison_memory_region(r + 20, 8);
  dump("poison [20,28) of malloc(64)", r, 0, 64);
  __asan_unpoison_memory_region(r, 64);

  std::printf("\n== 4. Rebuilt inline check vs __asan_region_is_poisoned\n");
  Tally heap_t, stack_t, global_t;
  compare(h, -16, 32, heap_t);
  stack_frame_compare(stack_t);
  compare(g13, -16, 48, global_t);
  std::printf("  heap   malloc(13): %d/%d aligned accesses agree\n", heap_t.agreed, heap_t.checked);
  std::printf("  stack  buf[10]:    %d/%d aligned accesses agree\n", stack_t.agreed, stack_t.checked);
  std::printf("  global g13[13]:    %d/%d aligned accesses agree\n", global_t.agreed,
              global_t.checked);
  const std::uintptr_t un = addr(h) + 6;  // 8-byte access covering bytes 6..13
  std::printf("  unaligned 8-byte access at +6: one-granule check %s, byte-exact check %s\n",
              toy_check_fails(un, 8) ? "fails" : "passes",
              __asan_region_is_poisoned(reinterpret_cast<void*>(un), 8) ? "fails" : "passes");

  std::printf("\n== 5. Free and quarantine\n");
  std::free(h);
  dump("malloc(13) after free [-16,+32)", h, -16, 32);
  bool reused = false;
  static void* later[1000];
  for (auto& slot : later) {
    slot = std::malloc(13);
    if (slot == h) reused = true;
  }
  for (auto* slot : later) std::free(slot);
  std::printf("  freed chunk returned by one of the next 1000 malloc(13) calls: %s\n",
              reused ? "yes" : "no");
  std::free(r);
  return 0;
}
