// In-process sampling front end. See sampler.hpp for the contract.
#include "sampler.hpp"

#include <atomic>
#include <cerrno>
#include <cinttypes>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <pthread.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <mach-o/dyld_images.h>
#include <mach-o/loader.h>
#include <mach/mach.h>
#elif defined(__linux__)
#include <link.h>
#include <ucontext.h>
#else
#error "mini_profiler supports macOS and Linux only"
#endif

namespace miniprof {
namespace {

constexpr unsigned kMaxDepth = 64;

struct Sample {
    std::uint32_t depth;
    std::uint32_t truncated; // 1 when the walk stopped at kMaxDepth with frames left
    std::uintptr_t pc[kMaxDepth];
};

struct Regs {
    std::uintptr_t pc, fp, sp;
};

// All state the handler touches is plain memory set up before the timer is armed.
std::vector<Sample> g_storage; // owns the samples; sized once in start()
Sample* g_samples = nullptr;   // what the handler reads: no container call inside a signal handler
unsigned g_capacity = 0;
std::atomic<unsigned> g_next{0};
std::atomic<unsigned> g_dropped{0};
std::uintptr_t g_stack_hi = 0; // one past the highest byte of the sampled thread's stack
unsigned g_interval_us = 0;
struct sigaction g_old_action; // what SIGPROF did before start(), put back by stop_and_write()

static_assert(std::atomic<unsigned>::is_always_lock_free, "the handler needs a lock-free counter");

inline Regs regs_from_ucontext(void* context) {
    auto* uc = static_cast<ucontext_t*>(context);
#if defined(__APPLE__) && defined(__aarch64__)
    const auto& ss = uc->uc_mcontext->__ss;
    return {static_cast<std::uintptr_t>(arm_thread_state64_get_pc(ss)),
            static_cast<std::uintptr_t>(arm_thread_state64_get_fp(ss)),
            static_cast<std::uintptr_t>(arm_thread_state64_get_sp(ss))};
#elif defined(__APPLE__) && defined(__x86_64__)
    const auto& ss = uc->uc_mcontext->__ss;
    return {static_cast<std::uintptr_t>(ss.__rip), static_cast<std::uintptr_t>(ss.__rbp),
            static_cast<std::uintptr_t>(ss.__rsp)};
#elif defined(__linux__) && defined(__aarch64__)
    const auto& mc = uc->uc_mcontext;
    return {static_cast<std::uintptr_t>(mc.pc), static_cast<std::uintptr_t>(mc.regs[29]),
            static_cast<std::uintptr_t>(mc.sp)};
#elif defined(__linux__) && defined(__x86_64__)
    const auto& gr = uc->uc_mcontext.gregs;
    return {static_cast<std::uintptr_t>(gr[REG_RIP]), static_cast<std::uintptr_t>(gr[REG_RBP]),
            static_cast<std::uintptr_t>(gr[REG_RSP])};
#else
#error "unsupported architecture: add the pc, frame pointer, and sp of its signal context here"
#endif
}

// A return address read from an AArch64 frame record may carry a pointer authentication code
// in its upper bits (-mbranch-protection=pac-ret signs x30 before saving it). XPACLRI strips
// the code from x30 and is a NOP on cores without the feature.
inline std::uintptr_t strip_pac(std::uintptr_t addr) {
#if defined(__aarch64__)
    register std::uintptr_t x30 __asm__("x30") = addr;
    __asm__("xpaclri" : "+r"(x30));
    return x30;
#else
    return addr;
#endif
}

// Frame-pointer walk. On both AArch64 and x86-64 a frame record is two words at the address
// in the frame pointer register: the caller's frame pointer, then the return address.
// Every load is bounds-checked against the sampled thread's stack, because in code built
// without frame pointers the register holds arbitrary data and a blind load can fault.
inline unsigned walk(const Regs& r, std::uintptr_t* out, std::uint32_t* truncated) {
    constexpr std::uintptr_t kRecord = 2 * sizeof(std::uintptr_t);
    unsigned n = 0;
    out[n++] = r.pc;
    std::uintptr_t fp = r.fp;
    std::uintptr_t lo = r.sp; // live frame records sit above the interrupted stack pointer
    *truncated = 0;
    for (;;) {
        if (fp < lo || fp > g_stack_hi - kRecord || (fp & (sizeof(std::uintptr_t) - 1)) != 0) {
            break;
        }
        const auto* record = reinterpret_cast<const std::uintptr_t*>(fp);
        const std::uintptr_t caller_fp = record[0];
        const std::uintptr_t return_address = strip_pac(record[1]);
        if (return_address == 0) {
            break;
        }
        if (n == kMaxDepth) {
            *truncated = 1;
            break;
        }
        out[n++] = return_address;
        if (caller_fp <= fp) {
            break; // 0 ends the chain; anything not strictly higher would loop
        }
        lo = fp + kRecord;
        fp = caller_fp;
    }
    return n;
}

// Async-signal-safe: no allocation, no locks, no stdio, errno preserved.
void on_sigprof(int, siginfo_t*, void* context) {
    const int saved_errno = errno;
    const unsigned slot = g_next.fetch_add(1, std::memory_order_relaxed);
    if (slot < g_capacity) {
        Sample& s = g_samples[slot];
        s.depth = walk(regs_from_ucontext(context), s.pc, &s.truncated);
    } else {
        g_dropped.fetch_add(1, std::memory_order_relaxed);
    }
    errno = saved_errno;
}

std::uintptr_t stack_top_of_this_thread() {
#if defined(__APPLE__)
    return reinterpret_cast<std::uintptr_t>(pthread_get_stackaddr_np(pthread_self()));
#else
    pthread_attr_t attr;
    void* base = nullptr;
    size_t size = 0;
    if (pthread_getattr_np(pthread_self(), &attr) != 0) {
        return 0;
    }
    pthread_attr_getstack(&attr, &base, &size);
    pthread_attr_destroy(&attr);
    return reinterpret_cast<std::uintptr_t>(base) + size;
#endif
}

struct Module {
    std::uintptr_t start = 0;   // runtime address of the first byte of the executable mapping
    std::uintptr_t end = 0;     // one past its last byte
    std::uint64_t file_offset = 0; // offset in the file of the byte mapped at start
    std::string identity;       // LC_UUID or GNU build ID in lowercase hex, "-" when absent
    std::string path;
    bool is_main = false;
};

std::string hex_of(const unsigned char* bytes, size_t n) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (size_t i = 0; i < n; ++i) {
        out.push_back(digits[bytes[i] >> 4]);
        out.push_back(digits[bytes[i] & 15]);
    }
    return out.empty() ? "-" : out;
}

#if defined(__APPLE__)

// Reads the __TEXT range and LC_UUID of an image from its Mach-O header in memory.
bool module_from_header(const mach_header_64* mh, std::intptr_t slide, Module* out) {
    if (mh == nullptr || mh->magic != MH_MAGIC_64) {
        return false;
    }
    const auto* lc = reinterpret_cast<const load_command*>(mh + 1);
    bool have_text = false;
    out->identity = "-";
    for (std::uint32_t i = 0; i < mh->ncmds; ++i) {
        if (lc->cmd == LC_SEGMENT_64) {
            const auto* seg = reinterpret_cast<const segment_command_64*>(lc);
            if (std::strncmp(seg->segname, "__TEXT", sizeof(seg->segname)) == 0) {
                out->start = static_cast<std::uintptr_t>(seg->vmaddr) + static_cast<std::uintptr_t>(slide);
                out->end = out->start + static_cast<std::uintptr_t>(seg->vmsize);
                out->file_offset = seg->fileoff;
                have_text = true;
            }
        } else if (lc->cmd == LC_UUID) {
            const auto* uuid = reinterpret_cast<const uuid_command*>(lc);
            out->identity = hex_of(uuid->uuid, sizeof(uuid->uuid));
        }
        lc = reinterpret_cast<const load_command*>(reinterpret_cast<const char*>(lc) + lc->cmdsize);
    }
    return have_text;
}

std::vector<Module> loaded_modules() {
    std::vector<Module> modules;
    const std::uint32_t count = _dyld_image_count();
    for (std::uint32_t i = 0; i < count; ++i) {
        Module m;
        const auto* mh = reinterpret_cast<const mach_header_64*>(_dyld_get_image_header(i));
        if (!module_from_header(mh, _dyld_get_image_vmaddr_slide(i), &m)) {
            continue;
        }
        m.path = _dyld_get_image_name(i);
        m.is_main = mh->filetype == MH_EXECUTE;
        modules.push_back(m);
    }
    // dyld itself is not in the _dyld_image_* list; the kernel reports it through TASK_DYLD_INFO.
    task_dyld_info_data_t info;
    mach_msg_type_number_t size = TASK_DYLD_INFO_COUNT;
    if (task_info(mach_task_self(), TASK_DYLD_INFO, reinterpret_cast<task_info_t>(&info), &size) == KERN_SUCCESS) {
        const auto* all = reinterpret_cast<const dyld_all_image_infos*>(info.all_image_info_addr);
        const auto* mh = reinterpret_cast<const mach_header_64*>(all->dyldImageLoadAddress);
        Module m;
        // A slide of 0 yields the unslid __TEXT address; the header address is the slid one.
        if (module_from_header(mh, 0, &m)) {
            const std::uintptr_t length = m.end - m.start;
            m.start = reinterpret_cast<std::uintptr_t>(mh);
            m.end = m.start + length;
            m.path = all->dyldPath != nullptr ? all->dyldPath : "/usr/lib/dyld";
            modules.push_back(m);
        }
    }
    return modules;
}

#else // Linux

int collect_module(struct dl_phdr_info* info, size_t, void* data) {
    auto* modules = static_cast<std::vector<Module>*>(data);
    std::string identity = "-";
    for (int i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr)& ph = info->dlpi_phdr[i];
        if (ph.p_type != PT_NOTE) {
            continue;
        }
        // A note is: namesz, descsz, type (4 bytes each), then name and desc, each padded to 4.
        const auto* p = reinterpret_cast<const unsigned char*>(info->dlpi_addr + ph.p_vaddr);
        const unsigned char* end = p + ph.p_memsz;
        while (p + 12 <= end) {
            std::uint32_t namesz, descsz, type;
            std::memcpy(&namesz, p, 4);
            std::memcpy(&descsz, p + 4, 4);
            std::memcpy(&type, p + 8, 4);
            const unsigned char* name = p + 12;
            const unsigned char* desc = name + ((namesz + 3u) & ~3u);
            if (desc + descsz > end) {
                break;
            }
            if (type == NT_GNU_BUILD_ID && namesz == 4 && std::memcmp(name, "GNU", 4) == 0) {
                identity = hex_of(desc, descsz);
            }
            p = desc + ((descsz + 3u) & ~3u);
        }
    }
    const bool is_main = modules->empty(); // dl_iterate_phdr reports the executable first
    for (int i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr)& ph = info->dlpi_phdr[i];
        if (ph.p_type != PT_LOAD || (ph.p_flags & PF_X) == 0) {
            continue;
        }
        Module m;
        m.start = static_cast<std::uintptr_t>(info->dlpi_addr + ph.p_vaddr);
        m.end = m.start + static_cast<std::uintptr_t>(ph.p_memsz);
        m.file_offset = ph.p_offset;
        m.identity = identity;
        m.is_main = is_main;
        if (info->dlpi_name != nullptr && info->dlpi_name[0] != '\0') {
            m.path = info->dlpi_name;
        } else if (is_main) {
            char buf[4096];
            const ssize_t len = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
            m.path = len > 0 ? std::string(buf, static_cast<size_t>(len)) : "[main]";
        } else {
            m.path = "[anonymous]";
        }
        modules->push_back(m);
    }
    return 0;
}

std::vector<Module> loaded_modules() {
    std::vector<Module> modules;
    dl_iterate_phdr(collect_module, &modules);
    return modules;
}

#endif

} // namespace

bool start(const Options& opts) {
    if (opts.interval_us == 0 || opts.capacity == 0) {
        errno = EINVAL; // an interval of 0 would disarm the timer instead of arming it
        return false;
    }
    // One process has one ITIMER_PROF and one SIGPROF disposition. If either is in use,
    // another profiler (or an earlier start()) owns them, and taking them over would break it.
    // This check comes first: nothing the handler reads may change while a handler can run.
    struct itimerval current;
    if (sigaction(SIGPROF, nullptr, &g_old_action) != 0 || getitimer(ITIMER_PROF, &current) != 0) {
        return false;
    }
    const bool timer_in_use = current.it_value.tv_sec != 0 || current.it_value.tv_usec != 0;
    const bool handler_in_use = (g_old_action.sa_flags & SA_SIGINFO) != 0 || g_old_action.sa_handler != SIG_DFL;
    if (timer_in_use || handler_in_use) {
        errno = EBUSY;
        return false;
    }

    const std::uintptr_t stack_hi = stack_top_of_this_thread();
    if (stack_hi == 0) {
        errno = ENOTSUP;
        return false;
    }
    g_storage.assign(opts.capacity, Sample{});
    g_samples = g_storage.data();
    g_capacity = opts.capacity;
    g_interval_us = opts.interval_us;
    g_stack_hi = stack_hi;
    g_next.store(0);
    g_dropped.store(0);

    struct sigaction sa;
    std::memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_sigprof;
    // SA_SIGINFO: the handler receives the interrupted register state.
    // SA_RESTART: a system call interrupted by the profiler is restarted instead of failing with EINTR.
    sa.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGPROF, &sa, nullptr) != 0) {
        return false;
    }

    struct itimerval timer;
    timer.it_interval.tv_sec = opts.interval_us / 1000000;
    timer.it_interval.tv_usec = opts.interval_us % 1000000;
    timer.it_value = timer.it_interval;
    if (setitimer(ITIMER_PROF, &timer, nullptr) != 0) {
        const int saved_errno = errno;
        sigaction(SIGPROF, &g_old_action, nullptr);
        errno = saved_errno;
        return false;
    }
    return true;
}

unsigned samples_taken() {
    const unsigned n = g_next.load(std::memory_order_relaxed);
    return n < g_capacity ? n : g_capacity;
}

bool stop_and_write(const char* raw_path) {
    struct itimerval off;
    std::memset(&off, 0, sizeof(off));
    setitimer(ITIMER_PROF, &off, nullptr);
    // A SIGPROF generated just before the timer was disarmed may still be pending. Restoring the
    // default action first would let it terminate the process; ignoring the signal discards it.
    signal(SIGPROF, SIG_IGN);
    sigaction(SIGPROF, &g_old_action, nullptr);

    const unsigned taken = samples_taken();
    const std::vector<Module> modules = loaded_modules();

    struct rusage usage;
    getrusage(RUSAGE_SELF, &usage);
    const long long cpu_us = (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) * 1000000LL +
                             usage.ru_utime.tv_usec + usage.ru_stime.tv_usec;

    std::FILE* out = std::fopen(raw_path, "w");
    if (out == nullptr) {
        return false;
    }
    std::fprintf(out, "miniprof-raw 1\n");
    std::fprintf(out, "source itimer-prof\n");
    std::fprintf(out, "interval_us %u\n", g_interval_us);
    std::fprintf(out, "cpu_us %lld\n", cpu_us);
    std::fprintf(out, "dropped %u\n", g_dropped.load());

    // Write only the modules that a sampled address falls in, the main executable always.
    for (const Module& m : modules) {
        bool hit = m.is_main;
        for (unsigned i = 0; i < taken && !hit; ++i) {
            for (unsigned d = 0; d < g_samples[i].depth && !hit; ++d) {
                hit = g_samples[i].pc[d] >= m.start && g_samples[i].pc[d] < m.end;
            }
        }
        if (hit) {
            std::fprintf(out, "map %" PRIxPTR " %" PRIxPTR " %" PRIx64 " %s %s %s\n", m.start, m.end, m.file_offset,
                         m.identity.c_str(), m.is_main ? "main" : "lib", m.path.c_str());
        }
    }
    for (unsigned i = 0; i < taken; ++i) {
        std::fprintf(out, "sample %u", g_samples[i].truncated);
        for (unsigned d = 0; d < g_samples[i].depth; ++d) {
            std::fprintf(out, " %" PRIxPTR, g_samples[i].pc[d]);
        }
        std::fputc('\n', out);
    }
    return std::fclose(out) == 0;
}

} // namespace miniprof
