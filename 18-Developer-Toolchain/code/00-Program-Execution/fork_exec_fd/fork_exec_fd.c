#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <pthread.h>
#include <spawn.h>
#include <errno.h>
#include <string.h>
#include <time.h>

extern char **environ;

struct vma_stats {
    long size_kb;
    long shared_dirty_kb;
    long private_dirty_kb;
};

static void die(const char *what) {
    perror(what);
    exit(1);
}

// Wait for pid and require a clean exit 0, so a failed child can never pass silently.
static void wait_ok(pid_t pid, const char *what) {
    int st = 0;
    if (waitpid(pid, &st, 0) != pid) die("waitpid");
    if (!WIFEXITED(st) || WEXITSTATUS(st) != 0) {
        fprintf(stderr, "%s: child did not exit 0 (wait status 0x%x)\n", what, st);
        exit(1);
    }
}

// Read the smaps entry of the single VMA that contains addr.
static struct vma_stats parse_smaps(void *addr) {
    struct vma_stats s = {-1, -1, -1};
    FILE *f = fopen("/proc/self/smaps", "r");
    if (!f) die("fopen /proc/self/smaps");
    char line[256];
    int match = 0;
    while (fgets(line, sizeof(line), f)) {
        unsigned long start, end;
        if (sscanf(line, "%lx-%lx", &start, &end) == 2) {
            match = (unsigned long)addr >= start && (unsigned long)addr < end;
        }
        if (match) {
            sscanf(line, "Size: %ld kB", &s.size_kb);
            sscanf(line, "Shared_Dirty: %ld kB", &s.shared_dirty_kb);
            sscanf(line, "Private_Dirty: %ld kB", &s.private_dirty_kb);
        }
    }
    fclose(f);
    return s;
}

static const char *get_basename(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

// Number of threads in this process, read with raw syscalls only (safe in a forked child).
static long thread_count(void) {
    char buf[4096];
    int fd = open("/proc/self/status", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = '\0';
    const char *t = strstr(buf, "Threads:");
    return t ? strtol(t + strlen("Threads:"), NULL, 10) : -1;
}

// Error-checking mutex: a timed lock by the owning thread fails with EDEADLK, by any other thread it times out.
static pthread_mutex_t mtx = PTHREAD_ERRORCHECK_MUTEX_INITIALIZER_NP;
static pthread_barrier_t locked_barrier;
static pthread_barrier_t release_barrier;

// Try to lock mtx for 50 ms and say what happened, so the output shows who owns it.
static const char *timedlock_result(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_nsec += 50 * 1000 * 1000;
    if (ts.tv_nsec >= 1000000000L) {
        ts.tv_sec += 1;
        ts.tv_nsec -= 1000000000L;
    }
    int rc = pthread_mutex_timedlock(&mtx, &ts);
    if (rc == 0) {
        pthread_mutex_unlock(&mtx);
        return "ACQUIRED";
    }
    if (rc == ETIMEDOUT) return "ETIMEDOUT (blocks)";
    if (rc == EDEADLK) return "EDEADLK (caller already owns it)";
    return "OTHER ERROR";
}

// A sibling thread that acquires mtx and holds it until the main thread lets it go.
static void *lock_holder(void *arg) {
    (void)arg;
    pthread_mutex_lock(&mtx);
    pthread_barrier_wait(&locked_barrier);
    pthread_barrier_wait(&release_barrier);
    pthread_mutex_unlock(&mtx);
    return NULL;
}

int main(int argc, char **argv) {
    // Disable buffering on stdout so fork never duplicates unflushed buffers
    setvbuf(stdout, NULL, _IONBF, 0);

    // Mode A: Invoked as execve / posix_spawn helper
    if (argc > 1 && strcmp(argv[1], "--helper") == 0) {
        int fd_cloexec = 3;
        int fd_inherited = 4;
        errno = 0;
        int f3 = fcntl(fd_cloexec, F_GETFD);
        int e3 = errno;
        int f4 = fcntl(fd_inherited, F_GETFD);
        char buf[5] = {0};
        ssize_t n = read(fd_inherited, buf, 4);
        printf("[execve helper] fd 3 (O_CLOEXEC): %s (errno=%d)\n",
               (f3 == -1 && e3 == EBADF) ? "CLOSED (EBADF)" : "OPEN", e3);
        printf("[execve helper] fd 4 (inherited): %s, read %zd bytes: \"%s\"\n",
               (f4 != -1) ? "OPEN" : "CLOSED", n, buf);
        return 0;
    }

    // Mode B: Invoked by kernel as shebang interpreter
    if (argc > 1 && strcmp(argv[1], "KERNEL_OPT") == 0) {
        for (int i = 0; i < argc; i++) {
            printf("  argv[%d] = %s\n", i, get_basename(argv[i]));
        }
        return 0;
    }

    // Any other argument is a mistake; refusing it also stops a broken shebang from re-running the driver.
    if (argc > 1) {
        fprintf(stderr, "unexpected argument: %s\n", argv[1]);
        return 2;
    }

    // Main test driver
    printf("=== 1. Kernel Copy-on-Write (COW) Page Semantics ===\n");
    size_t sz = 1024 * 1024; // 1 MiB
    size_t pg = (size_t)sysconf(_SC_PAGESIZE);
    // PROT_NONE guard pages on both sides keep the data VMA from merging with a neighbouring
    // writable anonymous VMA, so the smaps entry below covers exactly these 1 MiB.
    char *base = mmap(NULL, sz + 2 * pg, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (base == MAP_FAILED) die("mmap");
    char *p = base + pg;
    if (mprotect(p, sz, PROT_READ | PROT_WRITE) < 0) die("mprotect");
    memset(p, 0xAA, sz);

    struct vma_stats st = parse_smaps(p);
    if (st.size_kb != (long)(sz / 1024)) {
        fprintf(stderr, "smaps entry is %ld kB, expected exactly %zu kB\n", st.size_kb, sz / 1024);
        return 1;
    }
    printf("parent before fork:             Shared_Dirty=%4ld kB, Private_Dirty=%4ld kB\n",
           st.shared_dirty_kb, st.private_dirty_kb);

    pid_t pid = fork();
    if (pid < 0) die("fork");
    if (pid == 0) {
        st = parse_smaps(p);
        printf("child after fork, before write: Shared_Dirty=%4ld kB, Private_Dirty=%4ld kB\n",
               st.shared_dirty_kb, st.private_dirty_kb);
        // write to first 512 KiB
        memset(p, 0xBB, sz / 2);
        st = parse_smaps(p);
        printf("child after writing 512 KiB:    Shared_Dirty=%4ld kB, Private_Dirty=%4ld kB\n",
               st.shared_dirty_kb, st.private_dirty_kb);
        _exit(0);
    }
    wait_ok(pid, "section 1");
    st = parse_smaps(p);
    printf("parent after child exit:        Shared_Dirty=%4ld kB, Private_Dirty=%4ld kB\n",
           st.shared_dirty_kb, st.private_dirty_kb);

    printf("\n=== 2. File Descriptor Table vs Open File Description Sharing ===\n");
    char tmppath[] = "/tmp/lab_test_XXXXXX";
    int fd = mkstemp(tmppath);
    if (fd < 0) die("mkstemp");
    unlink(tmppath);
    if (write(fd, "0123456789ABCDEF", 16) != 16) die("write");
    lseek(fd, 0, SEEK_SET);

    pid = fork();
    if (pid < 0) die("fork");
    if (pid == 0) {
        char buf[5] = {0};
        if (read(fd, buf, 4) != 4) _exit(1);
        printf("child read 4 bytes: \"%s\"\n", buf);
        _exit(0);
    }
    wait_ok(pid, "section 2");
    off_t parent_pos = lseek(fd, 0, SEEK_CUR);
    printf("parent f_pos after child read: %ld (shared open file description)\n", (long)parent_pos);

    printf("\n=== 3. Descriptor Inheritance Across execve() and FD_CLOEXEC ===\n");
    // Move shared data file descriptor to 10 so it is not clobbered by 3 or 4
    if (dup2(fd, 10) < 0) die("dup2");
    close(fd);

    // Opened without O_CLOEXEC on purpose: the only thing that can close fd 3 across execve() below is the
    // explicit FD_CLOEXEC set here. (The lowest free descriptor is 3, so no dup2 is normally needed.)
    int fd_cl = open("/dev/null", O_RDONLY);
    if (fd_cl < 0) die("open");
    if (fd_cl != 3) {
        if (dup2(fd_cl, 3) < 0) die("dup2");
        close(fd_cl);
    }
    if (fcntl(3, F_SETFD, FD_CLOEXEC) < 0) die("fcntl");

    // Map our shared data file to fd 4 (without CLOEXEC)
    if (dup2(10, 4) < 0) die("dup2");
    if (fcntl(4, F_SETFD, 0) < 0) die("fcntl");

    pid = fork();
    if (pid < 0) die("fork");
    if (pid == 0) {
        char *exec_args[] = {argv[0], "--helper", NULL};
        execve(argv[0], exec_args, environ);
        _exit(1);
    }
    wait_ok(pid, "section 3");

    printf("\n=== 4. Multithreaded Fork Lock Contention / Deadlock Hazard ===\n");
    pthread_barrier_init(&locked_barrier, NULL, 2);
    pthread_barrier_init(&release_barrier, NULL, 2);
    pthread_t holder;
    if (pthread_create(&holder, NULL, lock_holder, NULL) != 0) {
        fprintf(stderr, "pthread_create failed\n");
        return 1;
    }
    pthread_barrier_wait(&locked_barrier); // the sibling thread now owns mtx
    printf("parent threads before fork: %ld\n", thread_count());
    printf("parent pthread_mutex_timedlock on mutex held by sibling thread: %s\n", timedlock_result());

    pid = fork();
    if (pid < 0) die("fork");
    if (pid == 0) {
        long child_threads = thread_count();
        int trylock_rc = pthread_mutex_trylock(&mtx);
        printf("child threads after fork: %ld\n", child_threads);
        printf("child pthread_mutex_trylock on mutex held by other thread: %s\n",
               trylock_rc == EBUSY ? "EBUSY (locked forever)" : "UNLOCKED");
        printf("child pthread_mutex_timedlock on mutex held by other thread: %s\n", timedlock_result());
        _exit(0);
    }
    wait_ok(pid, "section 4");
    pthread_barrier_wait(&release_barrier);
    pthread_join(holder, NULL);
    pthread_barrier_destroy(&locked_barrier);
    pthread_barrier_destroy(&release_barrier);

    printf("\n=== 5. Shebang (#! ) Argument Rewriting in Kernel ===\n");
    char script_name[] = "lab_shebang_script";
    FILE *sfp = fopen(script_name, "w");
    if (!sfp) die("fopen");
    fprintf(sfp, "#!%s KERNEL_OPT\n# payload\n", argv[0]);
    fclose(sfp);
    chmod(script_name, 0755);

    pid = fork();
    if (pid < 0) die("fork");
    if (pid == 0) {
        char *sargs[] = {script_name, "USER_ARG1", "USER_ARG2", NULL};
        execve(script_name, sargs, environ);
        _exit(1);
    }
    wait_ok(pid, "section 5");
    unlink(script_name);

    printf("\n=== 6. Safe Spawning with posix_spawn() and File Actions ===\n");
    int pipefds[2];
    if (pipe2(pipefds, O_CLOEXEC) < 0) die("pipe2");

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, pipefds[1], STDOUT_FILENO);
    posix_spawn_file_actions_addclose(&actions, pipefds[0]);
    posix_spawn_file_actions_addclose(&actions, pipefds[1]);

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);

    pid_t spawn_pid;
    char *spawn_args[] = {argv[0], "--helper", NULL};
    int spawn_rc = posix_spawn(&spawn_pid, argv[0], &actions, &attr, spawn_args, environ);
    if (spawn_rc != 0) {
        fprintf(stderr, "posix_spawn failed: %s\n", strerror(spawn_rc));
        return 1;
    }
    close(pipefds[1]);

    // Read until EOF: the helper prints twice, so a single read() could return a short count.
    char spawn_buf[1024] = {0};
    size_t used = 0;
    for (;;) {
        ssize_t r = read(pipefds[0], spawn_buf + used, sizeof(spawn_buf) - 1 - used);
        if (r < 0 && errno == EINTR) continue;
        if (r < 0) die("read");
        if (r == 0) break;
        used += (size_t)r;
        if (used == sizeof(spawn_buf) - 1) break;
    }
    close(pipefds[0]);
    wait_ok(spawn_pid, "section 6");
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attr);

    printf("posix_spawn status: %d, captured child stdout:\n", spawn_rc);
    printf("  %s", spawn_buf);

    return 0;
}
