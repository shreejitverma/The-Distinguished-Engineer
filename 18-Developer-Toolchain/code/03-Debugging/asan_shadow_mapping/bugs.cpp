// One memory bug per scenario name, so run.sh can show what ASan reports for each.
// Every bug touches memory through a volatile pointer or a noinline function so that the
// optimizer cannot delete the access or prove it out of bounds at compile time.

#include <sanitizer/asan_interface.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

char g13[13];

__attribute__((noinline)) char stack_read(int i) {
  char buf[10];
  std::memset(buf, 1, sizeof buf);
  volatile char* p = buf;
  return p[i];
}

__attribute__((noinline)) char* escape_stack_address() {
  char buf[16];
  std::memset(buf, 2, sizeof buf);
  char* volatile p = buf;  // launders the address so the compiler does not warn or fold it
  return p;
}

int run(const char* s) {
  if (!std::strcmp(s, "heap-overflow")) {
    volatile char* p = static_cast<char*>(std::malloc(13));
    return p[13];
  }
  if (!std::strcmp(s, "heap-underflow")) {
    volatile char* p = static_cast<char*>(std::malloc(13));
    return p[-1];
  }
  if (!std::strcmp(s, "use-after-free")) {
    volatile char* p = static_cast<char*>(std::malloc(13));
    std::free(const_cast<char*>(p));
    return p[0];
  }
  if (!std::strcmp(s, "double-free")) {
    char* p = static_cast<char*>(std::malloc(13));
    std::free(p);
    std::free(p);
    return 0;
  }
  if (!std::strcmp(s, "stale-write-after-reuse")) {
    char* p = static_cast<char*>(std::malloc(13));
    std::free(p);
    char* q = static_cast<char*>(std::malloc(13));
    std::printf("  new malloc(13) reuses the freed chunk: %s\n", q == p ? "yes" : "no");
    volatile char* stale = p;
    stale[0] = 'x';
    std::printf("  write through the stale pointer: not detected\n");
    std::free(q);
    return 0;
  }
  if (!std::strcmp(s, "stack-overflow")) return stack_read(10);
  if (!std::strcmp(s, "global-overflow")) {
    volatile char* p = g13;
    return p[13];
  }
  if (!std::strcmp(s, "use-after-scope")) {
    volatile int* p = nullptr;
    {
      int x = 7;
      p = &x;
    }
    return *p;
  }
  if (!std::strcmp(s, "use-after-return")) {
    volatile char* p = escape_stack_address();
    return p[0];
  }
  if (!std::strcmp(s, "container-overflow")) {
    std::vector<char> v;
    v.reserve(32);
    v.resize(5);
    volatile char* p = v.data();
    return p[5];  // inside the capacity, past the size
  }
  if (!std::strcmp(s, "user-poisoned")) {
    char* p = static_cast<char*>(std::malloc(64));
    ASAN_POISON_MEMORY_REGION(p + 16, 16);  // a no-op without ASan
    volatile char* q = p;
    return q[20];
  }
  if (!std::strcmp(s, "unaligned-cast")) {
    char* p = static_cast<char*>(std::malloc(13));
    std::memset(p, 0, 13);
    // UB: the cast tells the compiler the address is 8-aligned, so it emits one granule check.
    auto* q = reinterpret_cast<volatile std::uint64_t*>(p + 6);
    std::printf("  8-byte load through a misaligned uint64_t* at +6: not detected\n");
    return static_cast<int>(*q & 1);
  }
  if (!std::strcmp(s, "unaligned-memcpy")) {
    char* p = static_cast<char*>(std::malloc(13));
    std::memset(p, 0, 13);
    std::uint64_t v = 0;
    // No alignment promise: the copy is checked over its whole range (by __asan_memcpy, or by
    // first-byte and last-byte checks when the optimizer turns it into one unaligned load).
    std::memcpy(&v, p + 6, sizeof v);
    return static_cast<int>(v & 1);
  }
  std::fprintf(stderr, "unknown scenario %s\n", s);
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  std::setvbuf(stdout, nullptr, _IONBF, 0);  // ASan exits with _exit, which skips stdio flushing
  if (argc != 2) return 2;
  return run(argv[1]) & 0;
}
