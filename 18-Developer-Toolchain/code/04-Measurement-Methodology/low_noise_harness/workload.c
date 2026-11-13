/*
 * Benchmark target for the low-noise harness lab.
 *
 * Usage: workload ITERS
 *
 * Runs one untimed warm-up pass and REPS timed passes of a fixed integer kernel,
 * then prints key=value lines that let the harness check where and how it ran:
 * the CPUs it was allowed on, the CPU it started and ended on, involuntary
 * context switches during the timed passes, its personality flags, and the
 * address of a stack variable.
 */
#define _GNU_SOURCE
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <time.h>

#define REPS 50

/*
 * xorshift64: every iteration depends on the previous one, so the loop cannot be vectorized.
 * The seed differs on every call. With a fixed seed GCC proves the function pure, reuses the
 * warm-up result for the timed calls, and every timed pass measures 0 ns.
 */
__attribute__((noinline)) static uint64_t kernel(uint64_t iters, uint64_t seed)
{
    uint64_t x = 88172645463325252ULL + seed;
    uint64_t acc = 0;
    for (uint64_t i = 0; i < iters; i++) {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        acc += x;
    }
    return acc;
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static long involuntary_switches(void)
{
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return ru.ru_nivcsw;
}

static unsigned long personality_flags(void)
{
    unsigned long flags = 0;
    FILE *f = fopen("/proc/self/personality", "r");
    if (f != NULL) {
        if (fscanf(f, "%lx", &flags) != 1)
            flags = 0;
        fclose(f);
    }
    return flags;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s ITERS\n", argv[0]);
        return 2;
    }
    uint64_t iters = strtoull(argv[1], NULL, 10);
    volatile int stack_marker = 0;
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0) {
        perror("sched_getaffinity");
        return 1;
    }

    uint64_t checksum = kernel(iters, 0); /* warm-up: page faults, caches, branch predictors */
    int cpu_start = sched_getcpu();
    long switches_before = involuntary_switches();
    uint64_t ns[REPS];
    for (int r = 0; r < REPS; r++) {
        uint64_t t0 = now_ns();
        checksum += kernel(iters, (uint64_t)r + 1);
        ns[r] = now_ns() - t0;
    }
    long switches = involuntary_switches() - switches_before;
    int cpu_end = sched_getcpu();

    printf("allowed_cpus=%d\n", CPU_COUNT(&allowed));
    printf("cpu_start=%d\n", cpu_start);
    printf("cpu_end=%d\n", cpu_end);
    printf("nivcsw=%ld\n", switches);
    printf("personality=%lx\n", personality_flags());
    printf("stack=%p\n", (void *)&stack_marker);
    printf("checksum=%016llx\n", (unsigned long long)checksum);
    printf("ns=");
    for (int r = 0; r < REPS; r++)
        printf("%s%llu", r ? "," : "", (unsigned long long)ns[r]);
    printf("\n");
    return 0;
}
