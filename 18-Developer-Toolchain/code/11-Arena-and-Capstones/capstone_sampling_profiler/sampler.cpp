#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "sampler.hpp"
#include <atomic>
#include <cstring>
#include <csignal>
#include <ctime>
#include <pthread.h>
#include <sys/time.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <ucontext.h>

namespace profiler {

namespace {

RawSample g_samples[MAX_SAMPLES];
std::atomic<size_t> g_sample_count{0};
std::atomic<size_t> g_dropped_count{0};

uintptr_t g_stack_bottom = 0;
uintptr_t g_stack_top = 0;
uint64_t g_interval_us = 1000;

timer_t g_timerid;
bool g_use_posix_timer = false;
struct sigaction g_old_sa;

void sigprof_handler(int signo, siginfo_t* info, void* ucontext) {
    (void)signo;
    (void)info;
    if (!ucontext) return;

    ucontext_t* uc = static_cast<ucontext_t*>(ucontext);
    uintptr_t pc = 0;
    uintptr_t fp = 0;

#if defined(__x86_64__)
    pc = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RIP]);
    fp = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RBP]);
#elif defined(__aarch64__)
    pc = static_cast<uintptr_t>(uc->uc_mcontext.pc);
    fp = static_cast<uintptr_t>(uc->uc_mcontext.regs[29]);
    uintptr_t lr = static_cast<uintptr_t>(uc->uc_mcontext.regs[30]);
#endif

    // Strip PAC signature bits on AArch64 if pointer authentication is active
#if defined(__aarch64__)
    pc &= 0x0000ffffffffffffULL;
    fp &= 0x0000ffffffffffffULL;
    lr &= 0x0000ffffffffffffULL;
#endif

    size_t idx = g_sample_count.fetch_add(1, std::memory_order_relaxed);
    if (idx >= MAX_SAMPLES) {
        g_dropped_count.fetch_add(1, std::memory_order_relaxed);
        return;
    }

    RawSample& sample = g_samples[idx];
    size_t depth = 0;
    sample.ips[depth++] = pc;

    // Frame pointer walking loop
    uintptr_t curr_fp = fp;
    while (depth < MAX_STACK_DEPTH) {
        // Alignment check: frame pointer must be word-aligned
        if (curr_fp == 0 || (curr_fp & (sizeof(void*) - 1)) != 0) {
            break;
        }

        // Bounds check: must be strictly within thread stack bounds
        if (g_stack_bottom != 0 && g_stack_top != 0) {
            if (curr_fp < g_stack_bottom || curr_fp >= g_stack_top - (2 * sizeof(void*))) {
                break;
            }
        }

        uintptr_t* record = reinterpret_cast<uintptr_t*>(curr_fp);
        uintptr_t next_fp = record[0];
        uintptr_t ret_addr = record[1];

        // Monotonicity check: stack grows downward on x86_64 and AArch64
        if (next_fp <= curr_fp) {
            break;
        }
        if (ret_addr == 0) {
            break;
        }

#if defined(__aarch64__)
        ret_addr &= 0x0000ffffffffffffULL;
#endif

        sample.ips[depth++] = ret_addr;
        curr_fp = next_fp;
    }

    sample.depth = depth;
}

} // namespace

bool Sampler::init(uint64_t interval_us) {
    g_interval_us = interval_us;
    g_sample_count.store(0, std::memory_order_relaxed);
    g_dropped_count.store(0, std::memory_order_relaxed);

    // Retrieve thread stack bounds for safe validation in the signal handler
    pthread_attr_t attr;
    if (pthread_getattr_np(pthread_self(), &attr) == 0) {
        void* stack_addr = nullptr;
        size_t stack_size = 0;
        if (pthread_attr_getstack(&attr, &stack_addr, &stack_size) == 0) {
            g_stack_bottom = reinterpret_cast<uintptr_t>(stack_addr);
            g_stack_top = g_stack_bottom + stack_size;
        }
        pthread_attr_destroy(&attr);
    }

    // Install SIGPROF handler with SA_SIGINFO and SA_RESTART
    struct sigaction sa;
    std::memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = sigprof_handler;
    sa.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&sa.sa_mask);

    if (sigaction(SIGPROF, &sa, &g_old_sa) != 0) {
        return false;
    }

    return true;
}

bool Sampler::start() {
    // Attempt high-resolution per-thread POSIX timer first
    struct sigevent sev;
    std::memset(&sev, 0, sizeof(sev));
    sev.sigev_notify = SIGEV_THREAD_ID;
    sev.sigev_signo = SIGPROF;
#if defined(__linux__)
#if defined(sigev_notify_thread_id)
    sev.sigev_notify_thread_id = static_cast<pid_t>(syscall(SYS_gettid));
#else
    sev._sigev_un._tid = static_cast<pid_t>(syscall(SYS_gettid));
#endif
#endif

    if (timer_create(CLOCK_THREAD_CPUTIME_ID, &sev, &g_timerid) == 0) {
        g_use_posix_timer = true;
        struct itimerspec its;
        its.it_interval.tv_sec = g_interval_us / 1000000ULL;
        its.it_interval.tv_nsec = (g_interval_us % 1000000ULL) * 1000ULL;
        its.it_value = its.it_interval;
        if (timer_settime(g_timerid, 0, &its, nullptr) != 0) {
            timer_delete(g_timerid);
            g_use_posix_timer = false;
        }
    }

    if (!g_use_posix_timer) {
        // Fallback to process-wide setitimer
        struct itimerval itv;
        itv.it_interval.tv_sec = g_interval_us / 1000000ULL;
        itv.it_interval.tv_usec = g_interval_us % 1000000ULL;
        itv.it_value = itv.it_interval;
        if (setitimer(ITIMER_PROF, &itv, nullptr) != 0) {
            return false;
        }
    }

    return true;
}

void Sampler::stop() {
    if (g_use_posix_timer) {
        struct itimerspec its;
        std::memset(&its, 0, sizeof(its));
        timer_settime(g_timerid, 0, &its, nullptr);
        timer_delete(g_timerid);
        g_use_posix_timer = false;
    } else {
        struct itimerval itv;
        std::memset(&itv, 0, sizeof(itv));
        setitimer(ITIMER_PROF, &itv, nullptr);
    }

    sigaction(SIGPROF, &g_old_sa, nullptr);
}

std::vector<RawSample> Sampler::get_samples() {
    size_t count = g_sample_count.load(std::memory_order_relaxed);
    if (count > MAX_SAMPLES) count = MAX_SAMPLES;
    return std::vector<RawSample>(g_samples, g_samples + count);
}

size_t Sampler::dropped_samples() {
    return g_dropped_count.load(std::memory_order_relaxed);
}

} // namespace profiler
