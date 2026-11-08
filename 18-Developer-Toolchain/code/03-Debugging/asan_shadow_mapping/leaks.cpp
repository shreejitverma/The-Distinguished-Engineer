// LeakSanitizer reachability cases. Each allocation has a distinct size so the report can be
// read without symbolization:
//
//   size  how the last pointer is held                         expected
//   32    list head, pointer dropped                            direct leak
//   80    list tail, pointed to only by the 32-byte head        indirect leak
//   48    stored as (uintptr_t)p ^ 0xffff in a global           direct leak (hidden pointer)
//   40    stored only in an unregistered anonymous mmap page    direct leak (not a root)
//   56    stored only in an mmap page registered as a root      reachable
//   64    interior pointer (p + 32) stored in a global          reachable
//   24    pointer dropped after __lsan_ignore_object(p)         ignored

#include <sanitizer/lsan_interface.h>
#include <sys/mman.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

struct Node {
  Node* next;
  char pad[24];
};

void* g_interior;
std::uintptr_t g_hidden;

__attribute__((noinline)) void make_allocations(void** plain_page, void** root_page) {
  Node* head = static_cast<Node*>(std::malloc(sizeof(Node)));  // 32 bytes
  head->next = static_cast<Node*>(std::malloc(80));
  head->next->next = nullptr;
  head = nullptr;

  g_hidden = reinterpret_cast<std::uintptr_t>(std::malloc(48)) ^ 0xffff;

  *plain_page = std::malloc(40);
  *root_page = std::malloc(56);

  g_interior = static_cast<char*>(std::malloc(64)) + 32;

  void* ignored = std::malloc(24);
  __lsan_ignore_object(ignored);
}

// Overwrite the dead frame of make_allocations so stale copies of its pointers on the stack
// cannot keep the leaked chunks reachable (LSan scans whole stacks conservatively).
__attribute__((noinline)) void scrub_stack() {
  volatile char junk[16384];
  for (auto& c : junk) c = 0;
}

}  // namespace

int main() {
  const std::size_t page = 4096;
  void* plain = mmap(nullptr, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  void* root = mmap(nullptr, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  if (plain == MAP_FAILED || root == MAP_FAILED) return 3;
  __lsan_register_root_region(root, page);
  make_allocations(static_cast<void**>(plain), static_cast<void**>(root));
  scrub_stack();
  return 0;  // the leak check runs at exit
}
