/*
 * ingest.c - a message-ingest loop with three cache-line contention bugs and their fixes.
 *
 * Every worker thread is pinned to its own core and handles "messages" for a fixed wall-clock
 * time. Per message it reads its quota and bumps a counter. Where those two words live is the
 * whole drill:
 *
 *   array        per-worker shards packed in one array (16 bytes each): false sharing
 *   array-fixed  the same shards, each aligned to its own cache line
 *   heap         per-worker shards from consecutive malloc() calls: false sharing with no symbol
 *   heap-fixed   the same shards from aligned_alloc(FS_LINE, FS_LINE)
 *   total        every worker bumps one shared counter that already owns its line: true sharing
 *   total-fixed  workers count privately and flush to the shared counter every BATCH messages
 *
 * The program prints one line of key=value facts; run.sh and analyze.py turn them into checks.
 * Build: cc -O2 -g -pthread -DFS_LINE=<coherency line size> ingest.c -o ingest
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdalign.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifndef FS_LINE
#define FS_LINE 64
#endif
#define MAX_WORKERS 4
#define BATCH 4096
#define POLL 1024 /* messages between looks at the stop flag */

/* The hot per-worker state: two words, so four of them fit in one 64-byte line. */
struct shard {
    _Atomic uint64_t quota; /* read on every message, written once before the workers start */
    _Atomic uint64_t msgs;  /* written on every message, by exactly one worker */
};

/* The fix for a statically allocated shard: the type carries the alignment, so sizeof rounds up. */
struct shard_fixed {
    alignas(FS_LINE) _Atomic uint64_t quota;
    _Atomic uint64_t msgs;
};

_Static_assert(sizeof(struct shard_fixed) == FS_LINE, "a fixed shard must fill exactly one line");

/* A word that owns a whole line, so nothing else in the image can share it. */
struct lonely {
    alignas(FS_LINE) _Atomic uint64_t v;
};

/* The array itself is line-aligned; its elements are not. That is the bug in "array". */
static alignas(FS_LINE) struct shard shards[MAX_WORKERS];
static struct shard_fixed shards_fixed[MAX_WORKERS];
static struct shard_fixed private_quota[MAX_WORKERS]; /* per-worker quota for the "total" variants */
static struct lonely total;                          /* the one counter every worker bumps */
static struct lonely stop;                           /* read-mostly: stays Shared in every core */

enum kind { DIRECT, BATCHED };

struct lane {
    enum kind kind;
    int cpu;
    int index;
    _Atomic uint64_t *quota;
    _Atomic uint64_t *msgs;
    uint64_t handled;
    pthread_t thread;
};

static pthread_barrier_t start_line;

static int stopped(void)
{
    return atomic_load_explicit(&stop.v, memory_order_relaxed) != 0;
}

/*
 * The hot loop of every buggy variant, and of the two layout fixes.
 * The code is identical whether l->msgs is private to this worker or shared by all of them:
 * only the address differs, so only the data columns of a profile can tell the cases apart.
 * The counter is an atomic read-modify-write because a reporter thread may read it at any time.
 */
__attribute__((noinline)) static uint64_t ingest(struct lane *l)
{
    uint64_t handled = 0;

    while (!stopped()) {
        for (int i = 0; i < POLL; i++) {
            if (handled < atomic_load_explicit(l->quota, memory_order_relaxed))
                atomic_fetch_add_explicit(l->msgs, 1, memory_order_relaxed);
            handled++;
        }
    }
    return handled;
}

/* The fix for true sharing: write the shared line once per BATCH messages instead of every time. */
__attribute__((noinline)) static uint64_t ingest_batched(struct lane *l)
{
    uint64_t handled = 0;
    uint64_t pending = 0;

    while (!stopped()) {
        for (int i = 0; i < POLL; i++) {
            if (handled < atomic_load_explicit(l->quota, memory_order_relaxed)) {
                if (++pending == BATCH) {
                    atomic_fetch_add_explicit(l->msgs, pending, memory_order_relaxed);
                    pending = 0;
                }
            }
            handled++;
        }
    }
    atomic_fetch_add_explicit(l->msgs, pending, memory_order_relaxed);
    return handled;
}

static void *worker(void *arg)
{
    struct lane *l = arg;
    char name[16];
    cpu_set_t set;

    CPU_ZERO(&set);
    CPU_SET(l->cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        fprintf(stderr, "ingest: cannot pin worker %d to cpu %d: %s\n", l->index, l->cpu, strerror(errno));
        exit(2);
    }
    /* perf c2c report -c tid prints "tid:comm", so the name identifies the worker in the report. */
    snprintf(name, sizeof(name), "w%d", l->index);
    pthread_setname_np(pthread_self(), name);

    pthread_barrier_wait(&start_line);
    l->handled = l->kind == BATCHED ? ingest_batched(l) : ingest(l);
    return NULL;
}

static void die(const char *msg)
{
    fprintf(stderr, "ingest: %s\n", msg);
    exit(2);
}

static int parse_cpus(const char *list, int *cpus)
{
    int n = 0;
    char *copy = strdup(list);

    if (copy == NULL)
        die("out of memory");
    for (char *tok = strtok(copy, ","); tok != NULL; tok = strtok(NULL, ",")) {
        if (n == MAX_WORKERS)
            die("at most 4 cpus");
        cpus[n++] = atoi(tok);
    }
    free(copy);
    return n;
}

/* The [heap] mapping from /proc/self/maps, so a contended line can be placed in or out of it. */
static void print_heap_range(void)
{
    unsigned long lo = 0, hi = 0;
    char line[512];
    FILE *maps = fopen("/proc/self/maps", "r");

    if (maps == NULL)
        die("cannot read /proc/self/maps");
    while (fgets(line, sizeof(line), maps) != NULL) {
        if (strstr(line, "[heap]") != NULL && sscanf(line, "%lx-%lx", &lo, &hi) == 2)
            break;
    }
    fclose(maps);
    printf(" heap_lo=%lu heap_hi=%lu\n", lo, hi);
}

int main(int argc, char **argv)
{
    struct lane lanes[MAX_WORKERS];
    struct shard *heap_shards[MAX_WORKERS];
    int cpus[MAX_WORKERS];
    struct timespec t0, t1, nap;
    const char *variant;
    double secs;
    int workers;
    uint64_t counted = 0;
    uint64_t handled = 0;
    uint64_t slowest = UINT64_MAX;

    if (argc != 4) {
        fprintf(stderr, "usage: ingest <variant> <cpu,cpu[,cpu,cpu]> <seconds>\n");
        return 2;
    }
    variant = argv[1];
    workers = parse_cpus(argv[2], cpus);
    secs = atof(argv[3]);
    if (workers < 2)
        die("need at least 2 cpus");
    if (secs <= 0.0 || secs > 60.0)
        die("seconds must be in (0, 60]");

    memset(lanes, 0, sizeof(lanes));
    for (int i = 0; i < workers; i++) {
        struct lane *l = &lanes[i];

        l->index = i;
        l->cpu = cpus[i];
        if (strcmp(variant, "array") == 0) {
            l->kind = DIRECT;
            l->quota = &shards[i].quota;
            l->msgs = &shards[i].msgs;
        } else if (strcmp(variant, "array-fixed") == 0) {
            l->kind = DIRECT;
            l->quota = &shards_fixed[i].quota;
            l->msgs = &shards_fixed[i].msgs;
        } else if (strcmp(variant, "heap") == 0 || strcmp(variant, "heap-fixed") == 0) {
            /* All shards are allocated here, by one thread, back to back. */
            if (strcmp(variant, "heap") == 0)
                heap_shards[i] = malloc(sizeof(struct shard));
            else
                heap_shards[i] = aligned_alloc(FS_LINE, FS_LINE);
            if (heap_shards[i] == NULL)
                die("out of memory");
            atomic_init(&heap_shards[i]->msgs, 0);
            l->kind = DIRECT;
            l->quota = &heap_shards[i]->quota;
            l->msgs = &heap_shards[i]->msgs;
        } else if (strcmp(variant, "total") == 0 || strcmp(variant, "total-fixed") == 0) {
            l->kind = strcmp(variant, "total") == 0 ? DIRECT : BATCHED;
            l->quota = &private_quota[i].quota;
            l->msgs = &total.v;
        } else {
            die("unknown variant");
        }
        atomic_store(l->quota, UINT64_MAX);
    }

    if (pthread_barrier_init(&start_line, NULL, (unsigned)workers + 1) != 0)
        die("pthread_barrier_init failed");
    for (int i = 0; i < workers; i++) {
        if (pthread_create(&lanes[i].thread, NULL, worker, &lanes[i]) != 0)
            die("pthread_create failed");
    }
    pthread_barrier_wait(&start_line);
    clock_gettime(CLOCK_MONOTONIC, &t0);
    nap.tv_sec = (time_t)secs;
    nap.tv_nsec = (long)((secs - (double)nap.tv_sec) * 1e9);
    nanosleep(&nap, NULL);
    atomic_store(&stop.v, 1);
    for (int i = 0; i < workers; i++)
        pthread_join(lanes[i].thread, NULL);
    clock_gettime(CLOCK_MONOTONIC, &t1);

    printf("variant=%s workers=%d line=%d secs=%.3f", variant, workers, FS_LINE,
           (double)(t1.tv_sec - t0.tv_sec) + (double)(t1.tv_nsec - t0.tv_nsec) / 1e9);
    for (int i = 0; i < workers; i++) {
        handled += lanes[i].handled;
        if (lanes[i].handled < slowest)
            slowest = lanes[i].handled;
    }
    /* A shared counter is counted once; private counters are summed. */
    if (lanes[0].msgs != lanes[1].msgs) {
        for (int i = 0; i < workers; i++)
            counted += atomic_load(lanes[i].msgs);
    } else {
        counted = atomic_load(&total.v);
    }
    printf(" handled=%" PRIu64 " counted=%" PRIu64 " slowest=%" PRIu64, handled, counted, slowest);
    /* Decimal addresses: the harness scrubs 0x-prefixed hex, and analyze.py does the line math. */
    printf(" quota_addrs=");
    for (int i = 0; i < workers; i++)
        printf("%s%" PRIuPTR, i ? "," : "", (uintptr_t)lanes[i].quota);
    printf(" msgs_addrs=");
    for (int i = 0; i < workers; i++)
        printf("%s%" PRIuPTR, i ? "," : "", (uintptr_t)lanes[i].msgs);
    print_heap_range();
    return 0;
}
