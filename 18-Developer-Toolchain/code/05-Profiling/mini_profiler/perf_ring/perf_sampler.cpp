// Out-of-process sampling front end for Linux: perf_event_open(2) on a child process.
//
//   perf_sampler RAW_OUT PROGRAM [ARGS...]
//
// The kernel does the sampling and the frame-pointer walk. This program only opens one
// cpu-clock event on the child, maps its ring buffer, and decodes three record types:
//   PERF_RECORD_MMAP2   an executable mapping appeared (address range, file offset, path)
//   PERF_RECORD_SAMPLE  ip, pid/tid, and the callchain the kernel built
//   PERF_RECORD_EXIT    the child is gone
// It writes the same raw format as sampler.cpp, so symbolize.cpp serves both front ends.
#include <algorithm>
#include <cerrno>
#include <cinttypes>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <linux/perf_event.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

namespace {

constexpr std::uint64_t kPeriodNs = 1000000; // cpu-clock counts nanoseconds: one sample per 1 ms of CPU

struct Mapping {
    std::uint64_t addr, len, pgoff;
    std::string identity, path;
};

struct Capture {
    std::vector<Mapping> maps;
    std::vector<std::vector<std::uint64_t>> samples;
    std::uint64_t lost = 0;
    bool exit_seen = false;
};

std::string hex_of(const unsigned char* p, std::size_t n) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (std::size_t i = 0; i < n; ++i) {
        out.push_back(digits[p[i] >> 4]);
        out.push_back(digits[p[i] & 15]);
    }
    return out.empty() ? "-" : out;
}

template <typename T> T take(const unsigned char*& p) {
    T value;
    std::memcpy(&value, p, sizeof(T));
    p += sizeof(T);
    return value;
}

// Decodes one record. The field order of PERF_RECORD_SAMPLE is fixed by the kernel and
// depends only on attr.sample_type; this decoder is valid for IP | TID | CALLCHAIN.
// Records shorter than their fixed fields are ignored, and a count never reads past the record.
void decode(const perf_event_header& header, const unsigned char* body, Capture* cap) {
    const unsigned char* p = body;
    const std::size_t body_size = header.size - sizeof(header);
    switch (header.type) {
    case PERF_RECORD_SAMPLE: {
        if (body_size < 24) {
            break;
        }
        take<std::uint64_t>(p);                         // PERF_SAMPLE_IP
        take<std::uint32_t>(p);                         // PERF_SAMPLE_TID: pid
        take<std::uint32_t>(p);                         //                  tid
        auto nr = take<std::uint64_t>(p);               // PERF_SAMPLE_CALLCHAIN: count
        nr = std::min<std::uint64_t>(nr, (body_size - 24) / sizeof(std::uint64_t));
        std::vector<std::uint64_t> pcs;
        for (std::uint64_t i = 0; i < nr; ++i) {
            const auto ip = take<std::uint64_t>(p);
            // Values at or above PERF_CONTEXT_MAX are markers (PERF_CONTEXT_USER, ...), not addresses.
            if (ip < static_cast<std::uint64_t>(PERF_CONTEXT_MAX)) {
                pcs.push_back(ip);
            }
        }
        cap->samples.push_back(std::move(pcs));
        break;
    }
    case PERF_RECORD_MMAP2: {
        if (body_size < 64) { // pid, tid, addr, len, pgoff, 24 identity bytes, prot, flags
            break;
        }
        take<std::uint32_t>(p); // pid
        take<std::uint32_t>(p); // tid
        Mapping m;
        m.addr = take<std::uint64_t>(p);
        m.len = take<std::uint64_t>(p);
        m.pgoff = take<std::uint64_t>(p);
        // 24 bytes: either maj, min, ino, ino_generation, or a build ID when the misc bit says so.
        m.identity = "-";
        if ((header.misc & PERF_RECORD_MISC_MMAP_BUILD_ID) != 0) {
            const std::size_t size = std::min<std::size_t>(p[0], 20);
            m.identity = hex_of(p + 4, size);
        }
        p += 24;
        take<std::uint32_t>(p); // prot
        take<std::uint32_t>(p); // flags
        m.path = reinterpret_cast<const char*>(p);
        cap->maps.push_back(std::move(m));
        break;
    }
    case PERF_RECORD_LOST: {
        if (body_size < 16) {
            break;
        }
        take<std::uint64_t>(p); // id
        cap->lost += take<std::uint64_t>(p);
        break;
    }
    case PERF_RECORD_EXIT:
        cap->exit_seen = true;
        break;
    default:
        break; // PERF_RECORD_COMM, THROTTLE, ... are not needed here
    }
}

// Consumes every complete record between data_tail and data_head.
// The kernel writes at data_head and never overwrites unread data in this (non-overwrite) mode;
// the reader publishes its progress in data_tail. A record may wrap around the end of the buffer.
void drain(perf_event_mmap_page* meta, Capture* cap) {
    const auto* data = reinterpret_cast<const unsigned char*>(meta) + meta->data_offset;
    const std::uint64_t size = meta->data_size; // a power of two
    const std::uint64_t head = __atomic_load_n(&meta->data_head, __ATOMIC_ACQUIRE);
    std::uint64_t tail = meta->data_tail;
    std::vector<unsigned char> record;
    while (tail < head) {
        perf_event_header header;
        for (std::size_t i = 0; i < sizeof(header); ++i) {
            reinterpret_cast<unsigned char*>(&header)[i] = data[(tail + i) & (size - 1)];
        }
        if (header.size < sizeof(header) || tail + header.size > head) {
            break;
        }
        record.resize(header.size + 1u);
        for (std::size_t i = 0; i < header.size; ++i) {
            record[i] = data[(tail + i) & (size - 1)];
        }
        record[header.size] = 0; // terminates a filename that fills the record
        decode(header, record.data() + sizeof(header), cap);
        tail += header.size;
    }
    __atomic_store_n(&meta->data_tail, tail, __ATOMIC_RELEASE);
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s RAW_OUT PROGRAM [ARGS...]\n", argv[0]);
        return 2;
    }
    char resolved[PATH_MAX];
    if (realpath(argv[2], resolved) == nullptr) {
        std::perror(argv[2]);
        return 2;
    }

    // The child waits on a pipe until the event exists, so no instruction of the program is missed.
    int gate[2];
    if (pipe(gate) != 0) {
        std::perror("pipe");
        return 1;
    }
    const pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        close(gate[1]);
        char go;
        if (read(gate[0], &go, 1) != 1) {
            _exit(126);
        }
        close(gate[0]);
        execv(resolved, argv + 2);
        _exit(127);
    }
    close(gate[0]);

    perf_event_attr attr;
    std::memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.type = PERF_TYPE_SOFTWARE;
    attr.config = PERF_COUNT_SW_CPU_CLOCK;
    attr.sample_period = kPeriodNs;
    attr.sample_type = PERF_SAMPLE_IP | PERF_SAMPLE_TID | PERF_SAMPLE_CALLCHAIN;
    attr.disabled = 1;
    attr.enable_on_exec = 1; // counting starts at the child's execve
    attr.exclude_kernel = 1; // required for an unprivileged caller at perf_event_paranoid 2
    attr.exclude_hv = 1;
    attr.exclude_callchain_kernel = 1; // and so is this: no kernel frames in the callchain
    attr.mmap = 1;                     // executable mappings ...
    attr.mmap2 = 1;                    // ... as PERF_RECORD_MMAP2
    attr.build_id = 1;                 // with the build ID instead of device and inode, when the kernel has it
    attr.task = 1;                     // PERF_RECORD_FORK and PERF_RECORD_EXIT
    attr.watermark = 1;
    const long page = sysconf(_SC_PAGESIZE);
    std::size_t data_pages = 1;
    while (data_pages * 2 * static_cast<std::size_t>(page) <= 128 * 1024) {
        data_pages *= 2; // the data area must be a power-of-two number of pages
    }
    const std::size_t data_bytes = data_pages * static_cast<std::size_t>(page);
    attr.wakeup_watermark = static_cast<std::uint32_t>(data_bytes / 2);

    const int fd = static_cast<int>(
        syscall(SYS_perf_event_open, &attr, child, -1 /* any cpu */, -1 /* no group */, PERF_FLAG_FD_CLOEXEC));
    if (fd < 0) {
        const int saved = errno;
        std::fprintf(stderr, "perf_event_open: %s\n", std::strerror(saved));
        if (saved == EACCES || saved == EPERM) {
            std::fprintf(stderr, "check /proc/sys/kernel/perf_event_paranoid (2 or lower for this event) and, in a "
                                 "container, the seccomp profile\n");
        }
        kill(child, SIGKILL);
        waitpid(child, nullptr, 0);
        return 1;
    }
    // One metadata page, then the data area.
    const std::size_t map_bytes = (1 + data_pages) * static_cast<std::size_t>(page);
    void* base = mmap(nullptr, map_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (base == MAP_FAILED) {
        std::perror("mmap of the perf ring buffer");
        kill(child, SIGKILL);
        waitpid(child, nullptr, 0);
        return 1;
    }
    auto* meta = static_cast<perf_event_mmap_page*>(base);

    Capture cap;
    if (write(gate[1], "g", 1) != 1) {
        std::perror("write to the start gate");
        return 1;
    }
    close(gate[1]);

    int status = 0;
    for (;;) {
        pollfd pfd{fd, POLLIN, 0};
        poll(&pfd, 1, 100); // a wakeup at the watermark, or a 100 ms tick
        drain(meta, &cap);
        const pid_t reaped = waitpid(child, &status, WNOHANG);
        if (reaped == child) {
            break;
        }
        if (reaped < 0 && errno != EINTR) {
            std::perror("waitpid"); // for example ECHILD when SIGCHLD is ignored
            return 1;
        }
    }
    drain(meta, &cap);
    munmap(base, map_bytes);
    close(fd);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        std::fprintf(stderr, "perf_sampler: the target did not exit cleanly (wait status %d)\n", status);
        return 1;
    }

    std::FILE* out = std::fopen(argv[1], "w");
    if (out == nullptr) {
        std::perror(argv[1]);
        return 1;
    }
    std::size_t exec_maps = 0;
    for (const Mapping& m : cap.maps) {
        exec_maps += m.path == resolved ? 1 : 0;
    }
    std::fprintf(out, "miniprof-raw 1\nsource perf-event-open\ninterval_us %" PRIu64 "\n", kPeriodNs / 1000);
    std::fprintf(out, "lost %" PRIu64 "\nexec_maps %zu\nexit_seen %d\n", cap.lost, exec_maps, cap.exit_seen ? 1 : 0);
    for (const Mapping& m : cap.maps) {
        std::fprintf(out, "map %" PRIx64 " %" PRIx64 " %" PRIx64 " %s %s %s\n", m.addr, m.addr + m.len, m.pgoff,
                     m.identity.c_str(), m.path == resolved ? "main" : "lib", m.path.c_str());
    }
    for (const std::vector<std::uint64_t>& pcs : cap.samples) {
        std::fprintf(out, "sample 0");
        for (const std::uint64_t pc : pcs) {
            std::fprintf(out, " %" PRIx64, pc);
        }
        std::fputc('\n', out);
    }
    return std::fclose(out) == 0 ? 0 : 1;
}
