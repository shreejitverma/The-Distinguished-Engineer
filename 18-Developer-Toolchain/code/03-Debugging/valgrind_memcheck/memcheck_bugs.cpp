// One memory bug per case, selected by argv[1]; run.sh runs each case natively
// and under Memcheck. The binary is built with no sanitizer or instrumentation
// flags, so every report comes from Valgrind's binary translation alone.
//
// Line numbers matter: the expected output names them. Edit with care.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

#include <valgrind/memcheck.h>
#include <valgrind/valgrind.h>

volatile int g_sink;               // defeats dead-code elimination
static void* g_still_reachable;    // a live global pointer at exit
static char* g_interior;           // points into the middle of a block

__attribute__((noinline)) static void heap_overflow() {
    int* a = static_cast<int*>(std::malloc(10 * sizeof(int)));
    for (int i = 0; i < 10; ++i) a[i] = i;
    g_sink = a[10];                // one int past the end of a 40-byte block
    std::free(a);
}

__attribute__((noinline)) static void use_after_free() {
    int* p = new int[4]{1, 2, 3, 4};
    delete[] p;
    g_sink = p[2];                 // 8 bytes inside a freed 16-byte block
}

__attribute__((noinline)) static void uninit_branch() {
    int* p = static_cast<int*>(std::malloc(sizeof(int)));
    if (*p > 0) g_sink = 1;        // branch on a value malloc never defined
    std::free(p);
}

__attribute__((noinline)) static void bit_precision() {
    unsigned* p = static_cast<unsigned*>(std::malloc(sizeof(unsigned)));
    unsigned x = *p;               // all 32 bits undefined
    x |= 1u;                       // bit 0 is now a defined 1
    if (x & 1u) g_sink = 1;        // depends only on bit 0: no report
    if (x & 2u) g_sink = 2;        // depends on undefined bit 1: report
    std::free(p);
}

struct Padded {
    char tag;                      // offset 0, then 3 padding bytes
    int value;                     // offset 4
};

__attribute__((noinline)) static void padding_to_syscall() {
    Padded a;
    a.tag = 'x';
    a.value = 42;
    Padded b;
    std::memcpy(&b, &a, sizeof a); // copying undefined padding: no report
    int fd = open("/dev/null", O_WRONLY);
    ssize_t n = write(fd, &b, sizeof b); // the kernel sees the padding: report
    g_sink = static_cast<int>(n);
    close(fd);
}

__attribute__((noinline)) static void mismatched_delete() {
    int* p = new int[3];
    delete p;                      // new[] released with scalar delete
}

__attribute__((noinline)) static void double_free() {
    void* p = std::malloc(24);
    std::free(p);
    std::free(p);
}

struct Node {
    Node* next;
    char payload[24];              // sizeof(Node) == 32 on LP64
};

__attribute__((noinline)) static void make_leaks() {
    g_still_reachable = std::malloc(64);          // still reachable
    void* lost = std::malloc(16);                 // definitely lost
    g_sink = static_cast<int>(reinterpret_cast<unsigned long>(lost) & 1);
    Node* head = new Node{new Node{nullptr, {}}, {}};  // 32 definite + 32 indirect
    g_sink = static_cast<int>(reinterpret_cast<unsigned long>(head) & 1);
    char* block = static_cast<char*>(std::malloc(48));
    std::memset(block, 0x5a, 48);  // no leak-check heuristic matches this pattern
    g_interior = block + 16;       // only an interior pointer survives: possibly lost
}

alignas(16) static char g_arena[64];

__attribute__((noinline)) static void custom_pool() {
    // A toy bump allocator: Memcheck cannot see inside it unless told.
    char* obj = g_arena + 16;
    VALGRIND_MAKE_MEM_UNDEFINED(obj, 16);    // "allocate" 16 bytes
    obj[0] = 7;
    VALGRIND_MAKE_MEM_NOACCESS(obj, 16);     // "free" them
    g_sink = obj[0];                          // reported only because of the request
}

__attribute__((noinline)) static void client_check() {
    char buf[8];
    buf[0] = 'a';
    // Explicit assertion: are all 8 bytes defined? Seven are not.
    g_sink = static_cast<int>(VALGRIND_CHECK_MEM_IS_DEFINED(buf, sizeof buf) != 0);
}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    const char* c = argv[1];
    if (!std::strcmp(c, "probe")) {
        std::printf("RUNNING_ON_VALGRIND=%u\n", static_cast<unsigned>(RUNNING_ON_VALGRIND));
        return 0;
    }
    if (!std::strcmp(c, "heap_overflow")) heap_overflow();
    else if (!std::strcmp(c, "use_after_free")) use_after_free();
    else if (!std::strcmp(c, "uninit_branch")) uninit_branch();
    else if (!std::strcmp(c, "bit_precision")) bit_precision();
    else if (!std::strcmp(c, "padding_to_syscall")) padding_to_syscall();
    else if (!std::strcmp(c, "mismatched_delete")) mismatched_delete();
    else if (!std::strcmp(c, "double_free")) double_free();
    else if (!std::strcmp(c, "leaks")) make_leaks();
    else if (!std::strcmp(c, "custom_pool")) custom_pool();
    else if (!std::strcmp(c, "client_check")) client_check();
    else {
        std::fprintf(stderr, "unknown case %s\n", c);
        return 2;
    }
    return 0;
}
