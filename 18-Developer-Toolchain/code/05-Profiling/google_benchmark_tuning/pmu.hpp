// A user-space instruction and branch counter group over perf_event_open(2), Linux only.
//
// Same shape as the counter code in Google Benchmark (src/perf_counters.cc) and nanobench
// (LinuxPerformanceCounters): one group leader, members opened with group_fd = leader so the
// kernel schedules them together, user mode only (exclude_kernel, exclude_hv), and one read(2)
// of the whole group with PERF_FORMAT_GROUP. Unlike Google Benchmark v1.10.0 it also asks for
// TOTAL_TIME_ENABLED and TOTAL_TIME_RUNNING, as nanobench does, and refuses a reading taken
// while the group was multiplexed off the PMU instead of silently scaling or under-reporting it.
#pragma once

#if !defined(__linux__)
#error "pmu.hpp needs Linux perf_event_open(2)"
#endif

#include <linux/perf_event.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace lab {

struct Counts {
  std::uint64_t instructions = 0;
  std::uint64_t branches = 0;
};

class PmuGroup {
 public:
  PmuGroup() {
    leader_ = open_event(PERF_COUNT_HW_INSTRUCTIONS, -1);
    member_ = open_event(PERF_COUNT_HW_BRANCH_INSTRUCTIONS, leader_);
  }
  ~PmuGroup() {
    ::close(member_);
    ::close(leader_);
  }
  PmuGroup(const PmuGroup&) = delete;
  PmuGroup& operator=(const PmuGroup&) = delete;

  // Kept inline and short: everything between the ENABLE and DISABLE syscalls is counted.
  inline __attribute__((always_inline)) void start() {
    ::ioctl(leader_, PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP);
    ::ioctl(leader_, PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP);
  }
  inline __attribute__((always_inline)) void stop() {
    ::ioctl(leader_, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
  }

  Counts read_counts() {
    // PERF_FORMAT_GROUP | TOTAL_TIME_ENABLED | TOTAL_TIME_RUNNING layout:
    // nr, time_enabled, time_running, value[nr].
    struct {
      std::uint64_t nr, time_enabled, time_running, value[2];
    } buf{};
    const ssize_t got = ::read(leader_, &buf, sizeof buf);
    if (got != static_cast<ssize_t>(sizeof buf) || buf.nr != 2) {
      die("short read from the counter group");
    }
    // time_enabled and time_running accumulate across RESET, so compare the deltas.
    const std::uint64_t en = buf.time_enabled - last_enabled_;
    const std::uint64_t run = buf.time_running - last_running_;
    last_enabled_ = buf.time_enabled;
    last_running_ = buf.time_running;
    if (run != en) ++multiplexed_reads_;
    return Counts{buf.value[0], buf.value[1]};
  }

  std::uint64_t multiplexed_reads() const { return multiplexed_reads_; }

 private:
  static int open_event(std::uint64_t config, int group_fd) {
    perf_event_attr attr;
    std::memset(&attr, 0, sizeof attr);
    attr.size = sizeof attr;
    attr.type = PERF_TYPE_HARDWARE;
    attr.config = config;
    attr.disabled = group_fd == -1 ? 1 : 0;  // members follow the leader
    attr.exclude_kernel = 1;
    attr.exclude_hv = 1;
    attr.read_format =
        PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    const long fd = ::syscall(SYS_perf_event_open, &attr, 0 /*this thread*/, -1 /*any cpu*/,
                              group_fd, PERF_FLAG_FD_CLOEXEC);
    if (fd < 0) {
      std::fprintf(stderr,
                   "perf_event_open(PERF_TYPE_HARDWARE, config=%llu) failed: %s\n"
                   "Needs a PMU (bare metal or a VM with a virtual PMU) and "
                   "/proc/sys/kernel/perf_event_paranoid <= 2.\n",
                   static_cast<unsigned long long>(config), std::strerror(errno));
      std::exit(1);
    }
    return static_cast<int>(fd);
  }
  [[noreturn]] static void die(const char* what) {
    std::fprintf(stderr, "pmu: %s\n", what);
    std::exit(1);
  }

  int leader_ = -1;
  int member_ = -1;
  std::uint64_t last_enabled_ = 0;
  std::uint64_t last_running_ = 0;
  std::uint64_t multiplexed_reads_ = 0;
};

// Pin the thread to the CPU it is on, so every reading comes from one core type. On a CPU with
// several PMUs (big and little clusters), a generic PERF_TYPE_HARDWARE event is created for only
// one of them (Google Benchmark PR 2123), which "can cause inaccuracies" when the thread runs on
// the other cluster; a reading taken while the event could not run shows time_running < time_enabled.
inline void pin_to_current_cpu() {
  const int cpu = ::sched_getcpu();
  if (cpu < 0) return;
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(cpu, &set);
  if (::sched_setaffinity(0, sizeof set, &set) != 0) {
    std::fprintf(stderr, "warning: sched_setaffinity(cpu %d) failed; counts may span CPUs\n", cpu);
  }
}

}  // namespace lab
