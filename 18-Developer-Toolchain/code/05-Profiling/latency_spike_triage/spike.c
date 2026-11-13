/*
 * spike.c - a paced request loop with three tail-latency faults that can be switched on at run time.
 *
 * One "worker" thread serves 1000 requests per second on an open-loop schedule: request N is due at
 * start + N * 1 ms whether or not request N-1 has finished, so a stall delays every request behind it.
 * Latency is recorded twice: from the intended start (what a client sees) and from the actual start
 * (service time, which hides the queueing a stall causes).
 *
 * Faults, selected by a line on stdin ("fault NAME"):
 *   runq   - twelve "hog" threads pinned to the worker's CPU burn it for 80 ms in every 200 ms
 *   lock   - a "flusher" thread holds the mutex of the request path for 60 ms in every 200 ms
 *   oncpu  - one request in 250 takes a slow path that burns 60 ms of CPU
 *   none   - no fault
 *
 * Work is measured in CPU time, not loop iterations: a request spins until the thread's own CPU
 * clock has advanced by 100 us. That keeps the cost of a request the same on a fast core, on a
 * slow core, and in a VM whose host steals time, so the time split below is reproducible.
 *
 * Other commands: "reset" (start a new measurement window), "report" (print the window), "quit".
 * The program has no profiler and no probes inside it: the lab observes it from outside.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define INTERVAL_NS 1000000ULL /* one request per millisecond */
#define SLO_NS 20000000ULL     /* 20 ms */
#define MAX_SAMPLES 60000
#define HOGS 12
#define PERIOD_NS 200000000ULL    /* fault period: 200 ms */
#define HOG_BURST_NS 80000000ULL  /* hogs burn for 80 ms of each period */
#define LOCK_HOLD_NS 60000000ULL  /* flusher holds the mutex for 60 ms of each period */
#define REQUEST_CPU_NS 100000ULL  /* CPU time of a normal request: 100 us */
#define SLOW_PATH_EVERY 250       /* one request in 250 takes the slow path */
#define SLOW_PATH_CPU_NS 60000000ULL /* CPU time of the slow path: 60 ms */
#define SPIN_CHUNK 8192           /* loop iterations between two looks at the clock */

enum { FAULT_NONE, FAULT_RUNQ, FAULT_LOCK, FAULT_ONCPU };
enum { CMD_NONE, CMD_RESET, CMD_REPORT };

static atomic_int g_fault = FAULT_NONE;
static atomic_int g_cmd = CMD_NONE;
static atomic_int g_quit = 0;
static sem_t g_cmd_done;
static sem_t g_thread_ready; /* posted by each thread once it has a name and an affinity */

static pthread_mutex_t g_book_mu = PTHREAD_MUTEX_INITIALIZER; /* the lock on the request path */
static uint64_t g_book;

static pthread_mutex_t g_park_mu = PTHREAD_MUTEX_INITIALIZER; /* parks idle fault threads */
static pthread_cond_t g_park_cv = PTHREAD_COND_INITIALIZER;

static int g_cpu = -1;        /* CPU shared by the worker and the hogs */

static uint64_t g_lat_intended[MAX_SAMPLES];
static uint64_t g_lat_service[MAX_SAMPLES];
static size_t g_n;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void sleep_until(uint64_t deadline_ns) {
    struct timespec ts = {(time_t)(deadline_ns / 1000000000ULL), (long)(deadline_ns % 1000000000ULL)};
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {
    }
}

static uint64_t thread_cpu_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static inline __attribute__((always_inline)) uint64_t spin_chunk(uint64_t x) {
    for (int i = 0; i < SPIN_CHUNK; i++) {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
    }
    return x;
}

/* Spin until this thread has used cpu_ns more of CPU time. Inlined so the caller is the leaf frame. */
static inline __attribute__((always_inline)) uint64_t burn_cpu(uint64_t cpu_ns, uint64_t seed) {
    uint64_t x = seed | 1;
    uint64_t t0 = thread_cpu_ns();
    do {
        x = spin_chunk(x);
    } while (thread_cpu_ns() - t0 < cpu_ns);
    return x;
}

/* The request path. Each stage is a real frame so that a stack sampler can name it. */
__attribute__((noinline)) uint64_t parse_order(uint64_t id) { return burn_cpu(REQUEST_CPU_NS, id); }

__attribute__((noinline)) uint64_t revalidate_risk_limits(uint64_t id) {
    return burn_cpu(SLOW_PATH_CPU_NS, id);
}

__attribute__((noinline)) void book_update(uint64_t v) {
    pthread_mutex_lock(&g_book_mu);
    g_book ^= v;
    pthread_mutex_unlock(&g_book_mu);
}

__attribute__((noinline)) uint64_t handle_request(uint64_t id) {
    uint64_t v = parse_order(id);
    if (atomic_load_explicit(&g_fault, memory_order_relaxed) == FAULT_ONCPU && id % SLOW_PATH_EVERY == 0)
        v ^= revalidate_risk_limits(id);
    book_update(v);
    return v;
}

static void pin_to_shared_cpu(void) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(g_cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_setaffinity");
        exit(1);
    }
}

static int cmp_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

static void emit_report(void) {
    static uint64_t sorted[MAX_SAMPLES];
    size_t slow_intended = 0, slow_service = 0;
    for (size_t i = 0; i < g_n; i++) {
        slow_intended += g_lat_intended[i] > SLO_NS;
        slow_service += g_lat_service[i] > SLO_NS;
    }
    memcpy(sorted, g_lat_intended, g_n * sizeof(uint64_t));
    qsort(sorted, g_n, sizeof(uint64_t), cmp_u64);
    uint64_t p50 = g_n ? sorted[g_n / 2] : 0;
    uint64_t p99 = g_n ? sorted[(g_n * 99) / 100] : 0;
    uint64_t max = g_n ? sorted[g_n - 1] : 0;
    printf("report n=%zu p50_us=%" PRIu64 " p99_us=%" PRIu64 " max_us=%" PRIu64
           " slow_intended=%zu slow_service=%zu\n",
           g_n, p50 / 1000, p99 / 1000, max / 1000, slow_intended, slow_service);
    fflush(stdout);
}

__attribute__((noinline)) void *worker_main(void *arg) {
    pthread_setname_np(pthread_self(), "worker");
    pin_to_shared_cpu();
    sem_post(&g_thread_ready);
    uint64_t next = now_ns() + INTERVAL_NS;
    for (uint64_t id = 1; !atomic_load(&g_quit); id++) {
        int cmd = atomic_load(&g_cmd);
        if (cmd != CMD_NONE) {
            if (cmd == CMD_REPORT)
                emit_report();
            g_n = 0;
            atomic_store(&g_cmd, CMD_NONE);
            sem_post(&g_cmd_done);
            next = now_ns() + INTERVAL_NS; /* restart the schedule: the command is not a request */
        }
        sleep_until(next); /* returns at once when the schedule is already behind */
        uint64_t start = now_ns();
        handle_request(id);
        uint64_t end = now_ns();
        if (g_n < MAX_SAMPLES) {
            g_lat_intended[g_n] = end - next;
            g_lat_service[g_n] = end - start;
            g_n++;
        }
        next += INTERVAL_NS;
    }
    return arg;
}

/* Block until the wanted fault is active (or the program is quitting). */
static int wait_for_fault(int wanted) {
    pthread_mutex_lock(&g_park_mu);
    while (atomic_load(&g_fault) != wanted && !atomic_load(&g_quit))
        pthread_cond_wait(&g_park_cv, &g_park_mu);
    pthread_mutex_unlock(&g_park_mu);
    return !atomic_load(&g_quit);
}

__attribute__((noinline)) void *hog_main(void *arg) {
    pthread_setname_np(pthread_self(), "hog");
    pin_to_shared_cpu();
    sem_post(&g_thread_ready);
    volatile uint64_t sink = 0;
    while (wait_for_fault(FAULT_RUNQ)) {
        uint64_t t = now_ns();
        if (t % PERIOD_NS < HOG_BURST_NS)
            sink ^= spin_chunk(t | 1); /* a few microseconds of CPU, then look at the clock again */
        else
            sleep_until(t - t % PERIOD_NS + PERIOD_NS);
    }
    return arg;
}

__attribute__((noinline)) void *flusher_main(void *arg) {
    pthread_setname_np(pthread_self(), "flusher");
    sem_post(&g_thread_ready);
    while (wait_for_fault(FAULT_LOCK)) {
        uint64_t t = now_ns();
        pthread_mutex_lock(&g_book_mu);
        sleep_until(t + LOCK_HOLD_NS); /* stands in for slow I/O done while holding the lock */
        pthread_mutex_unlock(&g_book_mu);
        sleep_until(t + PERIOD_NS);
    }
    return arg;
}

static int last_allowed_cpu(void) {
    cpu_set_t set;
    if (sched_getaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_getaffinity");
        exit(1);
    }
    int cpu = -1;
    for (int i = 0; i < CPU_SETSIZE; i++)
        if (CPU_ISSET(i, &set))
            cpu = i;
    return cpu;
}

static void run_command(int cmd) {
    atomic_store(&g_cmd, cmd);
    sem_wait(&g_cmd_done);
}

int main(void) {
    /* Let the parent (the triage driver) and its children read our stacks where Yama restricts ptrace. */
    prctl(PR_SET_PTRACER, (unsigned long)getppid(), 0, 0, 0);
    sem_init(&g_cmd_done, 0, 0);
    sem_init(&g_thread_ready, 0, 0);
    g_cpu = last_allowed_cpu();

    pthread_t worker, flusher, hogs[HOGS];
    pthread_create(&worker, NULL, worker_main, NULL);
    pthread_create(&flusher, NULL, flusher_main, NULL);
    for (int i = 0; i < HOGS; i++)
        pthread_create(&hogs[i], NULL, hog_main, NULL);

    for (int i = 0; i < HOGS + 2; i++)
        sem_wait(&g_thread_ready); /* an observer that lists threads after "ready" sees their names */

    printf("ready pid=%d sys_futex=%d sys_clock_nanosleep=%d\n", getpid(), (int)SYS_futex,
           (int)SYS_clock_nanosleep);
    fflush(stdout);

    char line[128];
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\n")] = '\0';
        if (strncmp(line, "fault ", 6) == 0) {
            const char *name = line + 6;
            int fault = strcmp(name, "runq") == 0    ? FAULT_RUNQ
                        : strcmp(name, "lock") == 0  ? FAULT_LOCK
                        : strcmp(name, "oncpu") == 0 ? FAULT_ONCPU
                        : strcmp(name, "none") == 0  ? FAULT_NONE
                                                     : -1;
            if (fault < 0) {
                printf("error unknown fault\n");
            } else {
                pthread_mutex_lock(&g_park_mu);
                atomic_store(&g_fault, fault);
                pthread_cond_broadcast(&g_park_cv);
                pthread_mutex_unlock(&g_park_mu);
                printf("ok fault=%s\n", name);
            }
            fflush(stdout);
        } else if (strcmp(line, "reset") == 0) {
            run_command(CMD_RESET);
            printf("ok reset\n");
            fflush(stdout);
        } else if (strcmp(line, "report") == 0) {
            run_command(CMD_REPORT); /* the worker prints the report line */
        } else if (strcmp(line, "quit") == 0) {
            break;
        } else {
            printf("error unknown command\n");
            fflush(stdout);
        }
    }

    pthread_mutex_lock(&g_park_mu);
    atomic_store(&g_quit, 1);
    pthread_cond_broadcast(&g_park_cv);
    pthread_mutex_unlock(&g_park_mu);
    pthread_join(worker, NULL);
    pthread_join(flusher, NULL);
    for (int i = 0; i < HOGS; i++)
        pthread_join(hogs[i], NULL);
    return 0;
}
