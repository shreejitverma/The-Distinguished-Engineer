/*
 * pmc_lab.c - hardware performance counters through perf_event_open(2).
 *
 * Four experiments, each printing derived facts rather than raw counts, so the
 * output is the same on every run of a given Linux host class:
 *   1. one counter group {cycles, instructions, branches, branch-misses} read
 *      atomically with PERF_FORMAT_GROUP;
 *   2. the same branch loop over sorted and random bytes: equal instructions,
 *      very different branch-miss rate and IPC;
 *   3. dependent loads (pointer chase) against independent loads over 128 MiB;
 *   4. multiplexing: 48 flexible counters plus one pinned reference, scaled
 *      with value * time_enabled / time_running, and a short region in which
 *      some counters never run at all.
 *
 * Only generic PERF_TYPE_HARDWARE events are used, so the lab runs on Intel,
 * AMD, and Arm PMUs. Counting is user space only (exclude_kernel, exclude_hv),
 * which perf_event_paranoid <= 2 allows for a process measuring itself.
 * The process pins itself to one allowed CPU (a cpu_core CPU on Intel hybrid
 * parts) and opens every event with pid 0 and that CPU, so the kernel binds each
 * event to the PMU that covers the CPU: x86_pmu_event_init() and Arm's
 * armpmu_event_init() return -ENOENT for a CPU-bound event on another core type.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <linux/perf_event.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#define NGROUP 4
#define NMUX 48
#define BRANCH_BYTES (1u << 22)  /* 4 MiB of bytes for the branch loop */
#define CHASE_WORDS (1u << 25)   /* 32 Mi uint32 = 128 MiB for the chase */
#define CHASE_STEPS (1u << 22)   /* dependent loads per measurement */
#define UNIFORM_BYTES (1u << 20) /* all-taken bytes: a steady branch rate for [4] */
#define MUX_CYCLES 4             /* full rotation cycles the [4] region must span */
#define MUX_CAP_NS 10000000000ull /* stop [4] after 10 s of enabled time regardless */

static const char *const group_names[NGROUP] = {"cycles", "instructions", "branches",
                                                "branch-misses"};
static const uint64_t group_configs[NGROUP] = {
    PERF_COUNT_HW_CPU_CYCLES, PERF_COUNT_HW_INSTRUCTIONS, PERF_COUNT_HW_BRANCH_INSTRUCTIONS,
    PERF_COUNT_HW_BRANCH_MISSES};

struct group_read {
    uint64_t nr;
    uint64_t time_enabled;
    uint64_t time_running;
    uint64_t values[NGROUP];
};

struct single_read {
    uint64_t value;
    uint64_t time_enabled;
    uint64_t time_running;
};

struct counts {
    double cycles, instructions, branches, misses;
    int full_time; /* 1 when time_running == time_enabled */
};

static int all_full_time = 1;
static int g_cpu = -1; /* the CPU this process is pinned to */

__attribute__((noreturn)) static void die_errno(const char *what, int err) {
    fprintf(stderr, "pmc_lab: %s: %s\n", what, strerror(err));
    FILE *f = fopen("/proc/sys/kernel/perf_event_paranoid", "r");
    int paranoid = 0;
    if (f != NULL && fscanf(f, "%d", &paranoid) == 1) {
        fprintf(stderr, "pmc_lab: kernel.perf_event_paranoid = %d\n", paranoid);
        if (paranoid > 2)
            fprintf(stderr, "pmc_lab: upstream defines levels up to 2; a higher value comes from an "
                            "out-of-tree distribution patch that blocks unprivileged use; "
                            "'sudo sysctl kernel.perf_event_paranoid=2' allows self-monitoring\n");
    }
    if (f != NULL)
        fclose(f);
    if (err == EPERM)
        fprintf(stderr, "pmc_lab: EPERM usually means a seccomp filter; Docker's default profile "
                        "blocks perf_event_open\n");
    if (err == ENOENT || err == EOPNOTSUPP)
        fprintf(stderr, "pmc_lab: no hardware PMU is exposed here (container or VM without vPMU?)\n");
    exit(2);
}

__attribute__((noreturn)) static void die(const char *what) { die_errno(what, errno); }

__attribute__((noreturn)) static void fail(const char *msg) {
    fprintf(stderr, "pmc_lab: %s\n", msg);
    exit(2);
}

/* read() exactly n bytes; 0 bytes means a pinned event in error state. */
static void read_exact(int fd, void *buf, size_t n, const char *what) {
    ssize_t got = read(fd, buf, n);
    if (got < 0)
        die(what);
    if (got == 0) {
        fprintf(stderr, "pmc_lab: %s returned EOF: a pinned event could not be scheduled "
                        "(another pinned user holds the counters)\n", what);
        exit(2);
    }
    if ((size_t)got != n) {
        fprintf(stderr, "pmc_lab: %s: short read, %zd of %zu bytes\n", what, got, n);
        exit(2);
    }
}

static void report_nmi_watchdog(void) {
    FILE *f = fopen("/proc/sys/kernel/nmi_watchdog", "r");
    int v = 0;
    if (f != NULL && fscanf(f, "%d", &v) == 1)
        fprintf(stderr, "pmc_lab: kernel.nmi_watchdog = %d (when 1 it holds a counter)\n", v);
    if (f != NULL)
        fclose(f);
}

static int perf_open(struct perf_event_attr *attr, int group_fd) {
    /* glibc has no wrapper for perf_event_open(2). pid 0: this thread, only on g_cpu. */
    long fd = syscall(SYS_perf_event_open, attr, 0, g_cpu, group_fd, 0UL);
    return (int)fd;
}

static void base_attr(struct perf_event_attr *a, uint64_t config) {
    memset(a, 0, sizeof(*a));
    a->size = sizeof(*a);
    a->type = PERF_TYPE_HARDWARE;
    a->config = config;
    a->disabled = 1;
    a->exclude_kernel = 1;
    a->exclude_hv = 1;
}

/* Parse a sysfs cpulist such as "0-15,32-47" into set; 0 if the file is absent. */
static int read_cpulist(const char *path, cpu_set_t *set) {
    FILE *f = fopen(path, "r");
    if (f == NULL)
        return 0;
    CPU_ZERO(set);
    int lo, hi;
    while (fscanf(f, "%d", &lo) == 1) {
        hi = lo;
        int c = fgetc(f);
        if (c == '-') {
            if (fscanf(f, "%d", &hi) != 1)
                break;
            c = fgetc(f);
        }
        for (int cpu = lo; cpu <= hi && cpu < CPU_SETSIZE; cpu++)
            if (cpu >= 0)
                CPU_SET((size_t)cpu, set);
        if (c != ',')
            break;
    }
    fclose(f);
    return 1;
}

/*
 * Pin to one CPU the process is allowed to use. On Intel hybrid parts that CPU
 * must belong to cpu_core, whose PMU the generic events and the narrative use.
 */
static void pin_cpu(void) {
    cpu_set_t allowed, core;
    if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
        die("sched_getaffinity");
    if (read_cpulist("/sys/bus/event_source/devices/cpu_core/cpus", &core))
        CPU_AND(&allowed, &allowed, &core);
    int cpu = sched_getcpu();
    if (cpu < 0 || !CPU_ISSET((size_t)cpu, &allowed)) {
        cpu = -1;
        for (int c = 0; c < CPU_SETSIZE; c++)
            if (CPU_ISSET((size_t)c, &allowed)) {
                cpu = c;
                break;
            }
    }
    if (cpu < 0)
        fail("no allowed CPU (on a hybrid part: no allowed cpu_core CPU)");
    cpu_set_t one;
    CPU_ZERO(&one);
    CPU_SET((size_t)cpu, &one);
    if (sched_setaffinity(0, sizeof(one), &one) != 0)
        die("sched_setaffinity");
    g_cpu = cpu;
}

/* Largest perf_event_mux_interval_ms among the core PMUs; 10 ms if none is readable. */
static unsigned mux_interval_ms(void) {
    unsigned best = 0;
    DIR *d = opendir("/sys/bus/event_source/devices");
    if (d != NULL) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            const char *n = e->d_name;
            if (strcmp(n, "cpu") != 0 && strcmp(n, "cpu_core") != 0 && strcmp(n, "cpu_atom") != 0 &&
                strncmp(n, "armv8_", 6) != 0 && strncmp(n, "armv9_", 6) != 0)
                continue;
            char path[512];
            int len = snprintf(path, sizeof(path),
                               "/sys/bus/event_source/devices/%s/perf_event_mux_interval_ms", n);
            if (len < 0 || (size_t)len >= sizeof(path))
                continue;
            FILE *f = fopen(path, "r");
            unsigned v = 0;
            if (f != NULL && fscanf(f, "%u", &v) == 1 && v > best)
                best = v;
            if (f != NULL)
                fclose(f);
        }
        closedir(d);
    }
    return best ? best : 10;
}

/* ---- workloads ---------------------------------------------------------- */

static uint64_t rng_state = 0x9E3779B97F4A7C15ull;

static uint64_t xorshift64(void) {
    uint64_t x = rng_state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    rng_state = x;
    return x;
}

/*
 * The empty volatile asm in the taken arm forces a real conditional branch:
 * the compiler may not if-convert it to a conditional move or vectorize it,
 * because the asm statement must execute only when the condition holds.
 */
__attribute__((noinline)) static uint64_t branch_loop(const uint8_t *d, size_t n, int reps) {
    uint64_t s = 0;
    for (int r = 0; r < reps; r++) {
        for (size_t i = 0; i < n; i++) {
            if (d[i] >= 128) {
                s += d[i];
                __asm__ volatile("" : "+r"(s));
            }
        }
    }
    return s;
}

__attribute__((noinline)) static uint32_t chase(const uint32_t *next, uint32_t steps) {
    uint32_t p = 0;
    for (uint32_t i = 0; i < steps; i++)
        p = next[p];
    return p;
}

__attribute__((noinline)) static uint64_t stream_sum(const uint32_t *a, uint32_t n) {
    uint64_t s = 0;
    for (uint32_t i = 0; i < n; i++)
        s += a[i];
    return s;
}

static volatile uint64_t sink;

/* ---- experiment 1: a counter group -------------------------------------- */

static void open_group(int fds[NGROUP]) {
    struct perf_event_attr a;
    for (int i = 0; i < NGROUP; i++) {
        base_attr(&a, group_configs[i]);
        a.read_format =
            PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        a.disabled = (i == 0); /* members follow the leader */
        fds[i] = perf_open(&a, i == 0 ? -1 : fds[0]);
        if (fds[i] < 0) {
            int err = errno;
            char msg[64];
            snprintf(msg, sizeof(msg), "perf_event_open(%s)", group_names[i]);
            die_errno(msg, err);
        }
    }
}

static void group_start(const int fds[NGROUP]) {
    if (ioctl(fds[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) != 0 ||
        ioctl(fds[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP) != 0)
        die("ioctl(group start)");
}

static struct counts group_stop(const int fds[NGROUP]) {
    struct group_read g;
    if (ioctl(fds[0], PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP) != 0)
        die("ioctl(group stop)");
    read_exact(fds[0], &g, sizeof(g), "read(group)");
    if (g.nr != NGROUP)
        fail("group read returned the wrong number of values");
    struct counts c;
    c.cycles = (double)g.values[0];
    c.instructions = (double)g.values[1];
    c.branches = (double)g.values[2];
    c.misses = (double)g.values[3];
    c.full_time = g.time_running == g.time_enabled && g.time_running > 0;
    if (!c.full_time) {
        all_full_time = 0;
        fprintf(stderr, "pmc_lab: group ran %llu of %llu ns (cumulative over regions)\n",
                (unsigned long long)g.time_running, (unsigned long long)g.time_enabled);
        report_nmi_watchdog();
    }
    return c;
}

/* ---- experiment 4 helpers: one pinned reference and NMUX flexible counters ---- */

static void open_mux(int *ref, int mux[NMUX]) {
    struct perf_event_attr a;
    base_attr(&a, PERF_COUNT_HW_BRANCH_INSTRUCTIONS);
    a.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    a.pinned = 1; /* a pinned group is scheduled first and never rotated */
    *ref = perf_open(&a, -1);
    if (*ref < 0)
        die("perf_event_open(pinned reference)");
    a.pinned = 0;
    for (int i = 0; i < NMUX; i++) {
        mux[i] = perf_open(&a, -1);
        if (mux[i] < 0)
            die("perf_event_open(flexible counter)");
    }
}

static void close_mux(int ref, const int mux[NMUX]) {
    for (int i = 0; i < NMUX; i++)
        close(mux[i]);
    close(ref);
}

static const char *yes_no(int b) { return b ? "yes" : "no"; }

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    pin_cpu();

    uint8_t *random_bytes = malloc(BRANCH_BYTES);
    uint8_t *sorted_bytes = malloc(BRANCH_BYTES);
    uint8_t *uniform_bytes = malloc(UNIFORM_BYTES);
    uint32_t *next = malloc((size_t)CHASE_WORDS * sizeof(uint32_t));
    if (random_bytes == NULL || sorted_bytes == NULL || uniform_bytes == NULL || next == NULL) {
        fprintf(stderr, "pmc_lab: out of memory\n");
        return 2;
    }

    /* Random bytes, and the same multiset sorted by a counting sort. */
    size_t histogram[256] = {0};
    for (size_t i = 0; i < BRANCH_BYTES; i++) {
        random_bytes[i] = (uint8_t)(xorshift64() >> 56);
        histogram[random_bytes[i]]++;
    }
    for (size_t v = 0, k = 0; v < 256; v++)
        for (size_t j = 0; j < histogram[v]; j++)
            sorted_bytes[k++] = (uint8_t)v;
    memset(uniform_bytes, 0xFF, UNIFORM_BYTES);

    /* Sattolo's algorithm: one cycle through all CHASE_WORDS slots. */
    for (uint32_t i = 0; i < CHASE_WORDS; i++)
        next[i] = i;
    for (uint32_t i = CHASE_WORDS - 1; i > 0; i--) {
        uint32_t j = (uint32_t)(xorshift64() % i);
        uint32_t t = next[i];
        next[i] = next[j];
        next[j] = t;
    }

    printf("pmc_lab: perf_event_open, user space only (exclude_kernel=1, exclude_hv=1)\n");

    /* ---- experiments 1 and 2 ---- */
    int fds[NGROUP];
    open_group(fds);
    printf("[1] counter group {cycles, instructions, branches, branch-misses}\n");
    printf("  one read() returns nr = %d values plus time_enabled and time_running\n", NGROUP);

    sink = branch_loop(sorted_bytes, BRANCH_BYTES, 1); /* warm caches and predictors */
    group_start(fds);
    sink = branch_loop(sorted_bytes, BRANCH_BYTES, 20);
    struct counts sorted = group_stop(fds);
    group_start(fds);
    sink = branch_loop(random_bytes, BRANCH_BYTES, 20);
    struct counts rnd = group_stop(fds);

    double ipc_sorted = sorted.instructions / sorted.cycles;
    double ipc_random = rnd.instructions / rnd.cycles;
    double miss_sorted = sorted.misses / sorted.branches;
    double miss_random = rnd.misses / rnd.branches;
    double insn_ratio = rnd.instructions / sorted.instructions;
    fprintf(stderr, "pmc_lab: sorted IPC %.2f miss %.4f; random IPC %.2f miss %.4f\n", ipc_sorted,
            miss_sorted, ipc_random, miss_random);

    printf("[2] branch loop over the same 4 MiB of bytes, sorted then random\n");
    printf("  retired instructions equal within 2%%: %s\n",
           yes_no(insn_ratio > 0.98 && insn_ratio < 1.02));
    printf("  branch-miss rate, sorted input below 1%%: %s\n", yes_no(miss_sorted < 0.01));
    printf("  branch-miss rate, random input above 10%%: %s\n", yes_no(miss_random > 0.10));
    printf("  IPC sorted above 2x IPC random: %s\n", yes_no(ipc_sorted > 2.0 * ipc_random));

    /* ---- experiment 3 ---- */
    sink = stream_sum(next, CHASE_WORDS); /* touch every page before measuring */
    group_start(fds);
    sink = chase(next, CHASE_STEPS);
    struct counts dep = group_stop(fds);
    group_start(fds);
    sink = stream_sum(next, CHASE_STEPS);
    struct counts seq = group_stop(fds);

    double ipc_dep = dep.instructions / dep.cycles;
    double ipc_seq = seq.instructions / seq.cycles;
    double cpe_dep = dep.cycles / (double)CHASE_STEPS;
    fprintf(stderr, "pmc_lab: chase IPC %.3f, %.1f cycles/load; sequential IPC %.2f\n", ipc_dep,
            cpe_dep, ipc_seq);

    printf("[3] 4 Mi loads over a 128 MiB array: dependent chase, then sequential\n");
    printf("  IPC sequential above 4x IPC chase: %s\n", yes_no(ipc_seq > 4.0 * ipc_dep));
    printf("  cycles per dependent load above 20: %s\n", yes_no(cpe_dep > 20.0));
    printf("  group ran for its whole enabled time in every region: %s\n", yes_no(all_full_time));
    for (int i = 0; i < NGROUP; i++)
        close(fds[i]);

    /* ---- experiment 4: multiplexing ---- */
    int ref, mux[NMUX];
    open_mux(&ref, mux);

    struct single_read r, m[NMUX];
    /*
     * Long steady region, driven by time rather than by work: the kernel rotates one
     * flexible event per mux interval, so the region must span MUX_CYCLES full
     * rotations of NMUX events. Enabled counters may be read while they run.
     */
    const unsigned interval = mux_interval_ms();
    const uint64_t target_ns = (uint64_t)MUX_CYCLES * NMUX * interval * 1000000ull;
    if (prctl(PR_TASK_PERF_EVENTS_ENABLE, 0, 0, 0, 0) != 0)
        die("prctl(PR_TASK_PERF_EVENTS_ENABLE)");
    for (;;) {
        for (int chunk = 0; chunk < 16; chunk++)
            sink = branch_loop(uniform_bytes, UNIFORM_BYTES, 1);
        read_exact(ref, &r, sizeof(r), "read(reference)");
        if (r.time_enabled >= MUX_CAP_NS)
            break;
        if (r.time_enabled < target_ns)
            continue;
        int all_ran = 1;
        for (int i = 0; i < NMUX && all_ran; i++) {
            read_exact(mux[i], &m[i], sizeof(m[i]), "read(flexible counter)");
            all_ran = m[i].time_running > 0;
        }
        if (all_ran)
            break;
    }
    if (prctl(PR_TASK_PERF_EVENTS_DISABLE, 0, 0, 0, 0) != 0)
        die("prctl(PR_TASK_PERF_EVENTS_DISABLE)");
    read_exact(ref, &r, sizeof(r), "read(reference)");
    const int ref_ok = r.value > 0 && r.time_running > 0;
    int partial = 1, raw_low = ref_ok, scaled_close = ref_ok;
    double worst = 0.0;
    for (int i = 0; i < NMUX; i++) {
        read_exact(mux[i], &m[i], sizeof(m[i]), "read(flexible counter)");
        double ref_count = (double)r.value;
        if (m[i].time_running >= m[i].time_enabled)
            partial = 0;
        if (!((double)m[i].value < 0.9 * ref_count))
            raw_low = 0;
        if (m[i].time_running == 0) {
            scaled_close = 0;
            continue;
        }
        double scaled = (double)m[i].value * (double)m[i].time_enabled / (double)m[i].time_running;
        double err = scaled / ref_count - 1.0;
        if (err < 0)
            err = -err;
        if (err > worst)
            worst = err;
        if (!(err <= 0.05)) /* NaN-safe: a NaN error fails the check */
            scaled_close = 0;
    }
    fprintf(stderr,
            "pmc_lab: mux interval %u ms, region %.3f s, worst scaled error %.4f; "
            "counter 0 ran %.3f of enabled time\n",
            interval, (double)r.time_enabled / 1e9, worst,
            m[0].time_enabled ? (double)m[0].time_running / (double)m[0].time_enabled : 0.0);
    if (!ref_ok)
        fprintf(stderr, "pmc_lab: the pinned reference counted nothing; [4] checks fail closed\n");

    printf("[4] multiplexing: %d flexible branch counters plus 1 pinned reference, "
           "region of at least %d rotation cycles\n",
           NMUX, MUX_CYCLES);
    printf("  pinned reference ran its whole enabled time: %s\n",
           yes_no(r.time_running == r.time_enabled && r.time_running > 0));
    printf("  every flexible counter ran less than its enabled time: %s\n", yes_no(partial));
    printf("  every raw flexible count is more than 10%% below the reference: %s\n", yes_no(raw_low));
    printf("  every scaled count (value * enabled / running) within 5%% of the reference: %s\n",
           yes_no(scaled_close));

    /*
     * Short region: far shorter than one rotation tick, so the rotation never runs.
     * PERF_EVENT_IOC_RESET zeroes only the count, not time_enabled or time_running
     * (_perf_event_reset() in kernel/events/core.c), so fresh counters are opened.
     */
    close_mux(ref, mux);
    open_mux(&ref, mux);
    if (prctl(PR_TASK_PERF_EVENTS_ENABLE, 0, 0, 0, 0) != 0)
        die("prctl(PR_TASK_PERF_EVENTS_ENABLE)");
    sink = branch_loop(sorted_bytes, 1u << 16, 1);
    if (prctl(PR_TASK_PERF_EVENTS_DISABLE, 0, 0, 0, 0) != 0)
        die("prctl(PR_TASK_PERF_EVENTS_DISABLE)");
    int never_ran = 0;
    for (int i = 0; i < NMUX; i++) {
        read_exact(mux[i], &m[i], sizeof(m[i]), "read(flexible counter)");
        if (m[i].time_running == 0)
            never_ran++;
    }
    fprintf(stderr, "pmc_lab: short region: %d of %d counters never ran\n", never_ran, NMUX);
    printf("  64 KiB region: at least one counter has time_running == 0: %s\n", yes_no(never_ran > 0));

    close_mux(ref, mux);
    free(random_bytes);
    free(sorted_bytes);
    free(uniform_bytes);
    free(next);
    return 0;
}
