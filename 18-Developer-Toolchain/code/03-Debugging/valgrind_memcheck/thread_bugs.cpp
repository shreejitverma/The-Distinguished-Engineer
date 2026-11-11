// One threading bug (or its fix) per case, selected by argv[1]; run.sh runs each
// case under Helgrind and under DRD. Plain pthreads keep the happens-before
// edges visible: every edge the tools learn comes from an intercepted pthread call.
//
// Line numbers matter: the expected output names them. Edit with care.

#include <atomic>
#include <cstdio>
#include <cstring>
#include <pthread.h>
#include <unistd.h>

static int g_counter;                       // shared, sometimes unprotected
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_a = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_b = PTHREAD_MUTEX_INITIALIZER;
static int g_payload;                       // published through g_ready
static std::atomic<int> g_ready{0};

static void* racy_increment(void*) {
    for (int i = 0; i < 1000; ++i) ++g_counter;     // no lock: a data race
    return nullptr;
}

static void* locked_increment(void*) {
    for (int i = 0; i < 1000; ++i) {
        pthread_mutex_lock(&g_mu);
        ++g_counter;
        pthread_mutex_unlock(&g_mu);
    }
    return nullptr;
}

static void* lock_a_then_b(void*) {
    pthread_mutex_lock(&g_a);
    pthread_mutex_lock(&g_b);
    pthread_mutex_unlock(&g_b);
    pthread_mutex_unlock(&g_a);
    return nullptr;
}

static void* lock_b_then_a(void*) {
    pthread_mutex_lock(&g_b);
    pthread_mutex_lock(&g_a);               // inverts the order: potential deadlock
    pthread_mutex_unlock(&g_a);
    pthread_mutex_unlock(&g_b);
    return nullptr;
}

static void* publish(void*) {
    g_payload = 42;                                  // plain store
    g_ready.store(1, std::memory_order_release);     // correct C++ publication
    return nullptr;
}

static void* hold_lock(void*) {
    pthread_mutex_lock(&g_mu);
    usleep(100 * 1000);                     // hold the mutex for 100 ms
    pthread_mutex_unlock(&g_mu);
    return nullptr;
}

static void run_two(void* (*f1)(void*), void* (*f2)(void*)) {
    pthread_t t1, t2;
    pthread_create(&t1, nullptr, f1, nullptr);
    pthread_create(&t2, nullptr, f2, nullptr);
    pthread_join(t1, nullptr);
    pthread_join(t2, nullptr);
}

static void run_sequential(void* (*f1)(void*), void* (*f2)(void*)) {
    pthread_t t;
    pthread_create(&t, nullptr, f1, nullptr);
    pthread_join(t, nullptr);               // f1 finishes before f2 starts
    pthread_create(&t, nullptr, f2, nullptr);
    pthread_join(t, nullptr);
}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    const char* c = argv[1];
    if (!std::strcmp(c, "race")) {
        run_two(racy_increment, racy_increment);
    } else if (!std::strcmp(c, "locked")) {
        run_two(locked_increment, locked_increment);
    } else if (!std::strcmp(c, "lock_order")) {
        run_sequential(lock_a_then_b, lock_b_then_a);  // never deadlocks in this run
    } else if (!std::strcmp(c, "atomic_publish")) {
        pthread_t t;
        pthread_create(&t, nullptr, publish, nullptr);
        usleep(50 * 1000);                  // fixes report order only; not synchronization
        while (g_ready.load(std::memory_order_acquire) == 0) {
        }
        int seen = g_payload;               // ordered by release/acquire
        pthread_join(t, nullptr);
        if (seen != 42) return 1;
    } else if (!std::strcmp(c, "unlock_unlocked")) {
        pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
        pthread_mutex_unlock(&m);           // POSIX API misuse
    } else if (!std::strcmp(c, "contention")) {
        run_two(hold_lock, hold_lock);
    } else if (!std::strcmp(c, "atomic_counter")) {
        // The race case again, with the increment made one atomic read-modify-write.
        // Kept below every other case so that no recorded line number moves.
        static std::atomic<int> hits{0};
        auto bump = [](void*) -> void* {
            for (int i = 0; i < 1000; ++i) hits.fetch_add(1, std::memory_order_relaxed);
            return nullptr;
        };
        run_two(bump, bump);                // no pthread call orders these accesses
        if (hits.load(std::memory_order_relaxed) != 2000) return 1;
    } else {
        std::fprintf(stderr, "unknown case %s\n", c);
        return 2;
    }
    return 0;
}
