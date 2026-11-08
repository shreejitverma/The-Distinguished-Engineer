// ThreadSanitizer lab scenarios; argv[1] selects one, and each runs in its own process.
// Every scenario ends with the same program result, so only TSan decides the exit status.
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>

int counter = 0;
int payload = 0;
std::atomic<int> ready{0};
std::mutex mu;

void bump() { ++counter; }

void bump_locked() {
    std::lock_guard<std::mutex> guard(mu);
    ++counter;
}

// Two unsynchronized writers: no happens-before edge orders the two writes.
// The relaxed flag moves no clock; it only keeps the writes apart in time, because two
// first accesses that overlap by nanoseconds can both miss each other in the shadow cells.
static void racy_counter() {
    std::thread t([] {
        bump();
        ready.store(1, std::memory_order_relaxed);
    });
    while (ready.load(std::memory_order_relaxed) == 0) {
    }
    bump();
    t.join();
}

// The same writers behind a mutex: unlock releases, lock acquires.
static void mutex_counter() {
    std::thread t(bump_locked);
    bump_locked();
    t.join();
}

// The reader really runs after the writer, but a relaxed flag carries no clock.
static void relaxed_publish() {
    std::thread t([] {
        payload = 42;
        ready.store(1, std::memory_order_relaxed);
    });
    while (ready.load(std::memory_order_relaxed) == 0) {
    }
    int v = payload;
    t.join();
    std::printf("%d\n", v);
}

// Release store and acquire load: the reader joins the writer's vector clock.
static void acqrel_publish() {
    std::thread t([] {
        payload = 42;
        ready.store(1, std::memory_order_release);
    });
    while (ready.load(std::memory_order_acquire) == 0) {
    }
    int v = payload;
    t.join();
    std::printf("%d\n", v);
}

// Correct C++ (fence-fence synchronization), but the runtime does not model fences.
static void fence_publish() {
    std::thread t([] {
        payload = 42;
        std::atomic_thread_fence(std::memory_order_release);
        ready.store(1, std::memory_order_relaxed);
    });
    while (ready.load(std::memory_order_relaxed) == 0) {
    }
    std::atomic_thread_fence(std::memory_order_acquire);
    int v = payload;
    t.join();
    std::printf("%d\n", v);
}

// A 300 ms sleep orders the accesses in wall-clock time, not in happens-before.
// The flag is awaited before the sleep so the write always precedes the sleep, on any host load.
static void sleep_ordered() {
    std::thread t([] {
        payload = 7;
        ready.store(1, std::memory_order_relaxed);
    });
    while (ready.load(std::memory_order_relaxed) == 0) {
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    int v = payload;
    t.join();
    std::printf("%d\n", v);
}

int main(int argc, char **argv) {
    const char *which = argc > 1 ? argv[1] : "";
    if (std::strcmp(which, "racy_counter") == 0) {
        racy_counter();
    } else if (std::strcmp(which, "mutex_counter") == 0) {
        mutex_counter();
    } else if (std::strcmp(which, "relaxed_publish") == 0) {
        relaxed_publish();
    } else if (std::strcmp(which, "acqrel_publish") == 0) {
        acqrel_publish();
    } else if (std::strcmp(which, "fence_publish") == 0) {
        fence_publish();
    } else if (std::strcmp(which, "sleep_ordered") == 0) {
        sleep_ordered();
    } else {
        std::fprintf(stderr, "unknown scenario '%s'\n", which);
        return 2;
    }
    return 0;
}
