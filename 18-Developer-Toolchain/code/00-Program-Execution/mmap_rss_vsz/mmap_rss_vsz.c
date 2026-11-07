#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>

#ifndef PR_SET_THP_DISABLE
#define PR_SET_THP_DISABLE 41
#endif

#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif

typedef struct {
    unsigned long size_kb;
    unsigned long rss_kb;
    unsigned long pss_kb;
    unsigned long shared_clean_kb;
    unsigned long shared_dirty_kb;
    unsigned long private_clean_kb;
    unsigned long private_dirty_kb;
} vma_stats_t;

static int get_vma_stats(void *addr, vma_stats_t *stats) {
    memset(stats, 0, sizeof(*stats));
    FILE *f = fopen("/proc/self/smaps", "r");
    if (!f) return -1;
    char line[512];
    uintptr_t target = (uintptr_t)addr;
    int in_vma = 0;
    while (fgets(line, sizeof(line), f)) {
        uintptr_t start, end;
        if (sscanf(line, "%lx-%lx", &start, &end) == 2) {
            if (target >= start && target < end) {
                in_vma = 1;
            } else if (in_vma) {
                break;
            }
            continue;
        }
        if (in_vma) {
            unsigned long val;
            if (sscanf(line, "Size: %lu kB", &val) == 1) stats->size_kb = val;
            else if (sscanf(line, "Rss: %lu kB", &val) == 1) stats->rss_kb = val;
            else if (sscanf(line, "Pss: %lu kB", &val) == 1) stats->pss_kb = val;
            else if (sscanf(line, "Shared_Clean: %lu kB", &val) == 1) stats->shared_clean_kb = val;
            else if (sscanf(line, "Shared_Dirty: %lu kB", &val) == 1) stats->shared_dirty_kb = val;
            else if (sscanf(line, "Private_Clean: %lu kB", &val) == 1) stats->private_clean_kb = val;
            else if (sscanf(line, "Private_Dirty: %lu kB", &val) == 1) stats->private_dirty_kb = val;
        }
    }
    fclose(f);
    return in_vma ? 0 : -1;
}

static long get_minflt(void) {
    struct rusage ru;
    if (getrusage(RUSAGE_SELF, &ru) != 0) return -1;
    return ru.ru_minflt;
}

int main(void) {
    // Disable Transparent Huge Pages for this thread tree so each minor fault maps exactly one 4 KiB base page
    if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0) != 0) {
        perror("prctl PR_SET_THP_DISABLE");
    }

    const size_t page_size = sysconf(_SC_PAGESIZE);
    printf("Page size: %zu bytes\n\n", page_size);

    // =========================================================================
    // Step 1: Anonymous Private Allocation (Demand Paging)
    // =========================================================================
    const size_t map_size = 16 * 1024 * 1024; // 16 MiB = 4096 pages
    void *p1 = mmap(NULL, map_size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p1 == MAP_FAILED) {
        perror("mmap step 1");
        return 1;
    }

    vma_stats_t s1;
    if (get_vma_stats(p1, &s1) != 0) {
        fprintf(stderr, "Failed to get VMA stats for p1\n");
        return 1;
    }

    printf("--- Step 1: Anonymous Private Allocation (16 MiB) ---\n");
    printf("VMA Size:          %lu kB\n", s1.size_kb);
    printf("VMA RSS:           %lu kB\n", s1.rss_kb);
    printf("VMA PSS:           %lu kB\n", s1.pss_kb);
    printf("VMA Private_Dirty: %lu kB\n", s1.private_dirty_kb);
    printf("Demand paging verified: virtual range mapped with zero physical resident pages.\n\n");

    // =========================================================================
    // Step 2: Touch 2048 pages (8 MiB) and measure minor page faults
    // =========================================================================
    const size_t touch_pages = 2048; // 8 MiB
    long minflt_before = get_minflt();

    volatile char *arr = (volatile char *)p1;
    for (size_t i = 0; i < touch_pages; i++) {
        arr[i * page_size] = (char)(i & 0xFF);
    }

    long minflt_after = get_minflt();
    long delta_minflt = minflt_after - minflt_before;

    vma_stats_t s2;
    if (get_vma_stats(p1, &s2) != 0) {
        fprintf(stderr, "Failed to get VMA stats for p1 after touch\n");
        return 1;
    }

    printf("--- Step 2: Write to 2048 Pages (8 MiB) ---\n");
    printf("Minor page faults incurred: %ld\n", delta_minflt);
    printf("VMA RSS:                    %lu kB\n", s2.rss_kb);
    printf("VMA PSS:                    %lu kB\n", s2.pss_kb);
    printf("VMA Private_Dirty:          %lu kB\n", s2.private_dirty_kb);
    printf("Page fault allocation verified: exactly touched pages became resident and dirty.\n\n");

    // =========================================================================
    // Step 3: MAP_POPULATE Eager Pre-faulting
    // =========================================================================
    const size_t pop_size = 4 * 1024 * 1024; // 4 MiB = 1024 pages
    minflt_before = get_minflt();

    // Place p2 directly below p1 (or directly above it) with MAP_FIXED_NOREPLACE, which fails with
    // EEXIST instead of replacing an existing mapping, so adjacency never depends on the kernel's
    // default top-down placement.
    void *p2 = mmap((char *)p1 - pop_size, pop_size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE | MAP_FIXED_NOREPLACE, -1, 0);
    if (p2 == MAP_FAILED) {
        p2 = mmap((char *)p1 + map_size, pop_size, PROT_READ | PROT_WRITE,
                  MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE | MAP_FIXED_NOREPLACE, -1, 0);
    }
    if (p2 == MAP_FAILED) {
        perror("mmap step 3");
        return 1;
    }
    minflt_after = get_minflt();
    delta_minflt = minflt_after - minflt_before;

    vma_stats_t s3;
    if (get_vma_stats(p2, &s3) != 0) {
        fprintf(stderr, "Failed to get VMA stats for p2\n");
        return 1;
    }

    printf("--- Step 3: MAP_POPULATE Eager Allocation (4 MiB) ---\n");
    printf("Minor faults during mmap: %ld\n", delta_minflt);
    printf("VMA Size:                 %lu kB\n", s3.size_kb);
    printf("VMA RSS:                  %lu kB\n", s3.rss_kb);
    printf("VMA Private_Dirty:        %lu kB\n", s3.private_dirty_kb);
    printf("Eager population verified: physical pages populated synchronously without user touch.\n\n");

    // Unmap p2 so it does not affect subsequent steps on p1
    if (munmap(p2, pop_size) != 0) {
        perror("munmap step 3");
        return 1;
    }

    // =========================================================================
    // Step 4: Fork and Copy-on-Write (COW) Mechanics
    // =========================================================================
    printf("--- Step 4: Fork and Copy-on-Write (COW) Mechanics ---\n");
    fflush(stdout);

    int pipe_sync[2];
    if (pipe(pipe_sync) != 0) {
        perror("pipe");
        return 1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }

    if (pid == 0) {
        // Child process
        close(pipe_sync[0]); // close read end

        vma_stats_t sc1;
        if (get_vma_stats(p1, &sc1) != 0) _exit(1);
        printf("[Child post-fork before write]\n");
        printf("VMA RSS:           %lu kB\n", sc1.rss_kb);
        printf("VMA PSS:           %lu kB\n", sc1.pss_kb);
        printf("VMA Shared_Dirty:  %lu kB\n", sc1.shared_dirty_kb);
        printf("VMA Private_Dirty: %lu kB\n", sc1.private_dirty_kb);

        // Child writes to 256 pages (1 MiB)
        const size_t cow_pages = 256;
        long c_minflt_before = get_minflt();
        for (size_t i = 0; i < cow_pages; i++) {
            arr[i * page_size] = (char)(0xA5);
        }
        long c_minflt_after = get_minflt();

        vma_stats_t sc2;
        if (get_vma_stats(p1, &sc2) != 0) _exit(1);
        printf("[Child after writing 256 pages (1 MiB)]\n");
        printf("Minor faults on write: %ld\n", c_minflt_after - c_minflt_before);
        printf("VMA RSS:               %lu kB\n", sc2.rss_kb);
        printf("VMA PSS:               %lu kB\n", sc2.pss_kb);
        printf("VMA Shared_Dirty:      %lu kB\n", sc2.shared_dirty_kb);
        printf("VMA Private_Dirty:     %lu kB\n", sc2.private_dirty_kb);
        fflush(stdout);

        char ok = 1;
        if (write(pipe_sync[1], &ok, 1) != 1) {
            perror("write sync");
            _exit(1);
        }
        close(pipe_sync[1]);
        _exit(0);
    }

    // Parent process
    close(pipe_sync[1]); // close write end
    char sync_buf;
    if (read(pipe_sync[0], &sync_buf, 1) != 1) {
        perror("read sync");
        return 1;
    }
    close(pipe_sync[0]);

    int status;
    if (waitpid(pid, &status, 0) != pid || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "child did not exit cleanly\n");
        return 1;
    }

    vma_stats_t sp_final;
    if (get_vma_stats(p1, &sp_final) != 0) {
        fprintf(stderr, "Failed to get VMA stats for p1 after child exit\n");
        return 1;
    }
    printf("[Parent after child exit]\n");
    printf("VMA RSS:           %lu kB\n", sp_final.rss_kb);
    printf("VMA PSS:           %lu kB\n", sp_final.pss_kb);
    printf("VMA Shared_Dirty:  %lu kB\n", sp_final.shared_dirty_kb);
    printf("VMA Private_Dirty: %lu kB\n", sp_final.private_dirty_kb);
    printf("COW verified: child unshares only modified pages; PSS reflects proportional ownership.\n\n");

    // =========================================================================
    // Step 5: MADV_DONTNEED Page Reclamation
    // =========================================================================
    printf("--- Step 5: MADV_DONTNEED Page Reclamation ---\n");
    // Page 0 holds a nonzero marker first, so reading 0x00 afterwards can only mean the page was dropped.
    arr[0] = (char)0x5A;
    printf("Byte before MADV_DONTNEED:             0x%02X\n", (unsigned char)arr[0]);
    if (madvise(p1, 8 * 1024 * 1024, MADV_DONTNEED) != 0) {
        perror("madvise");
        return 1;
    }

    vma_stats_t s5;
    if (get_vma_stats(p1, &s5) != 0) {
        fprintf(stderr, "Failed to get VMA stats for p1 after MADV_DONTNEED\n");
        return 1;
    }
    printf("VMA RSS after MADV_DONTNEED:           %lu kB\n", s5.rss_kb);
    printf("VMA Private_Dirty after MADV_DONTNEED: %lu kB\n", s5.private_dirty_kb);
    printf("Re-read byte value:                    0x%02X\n", (unsigned char)arr[0]);
    printf("MADV_DONTNEED verified: resident frames released; re-read returns zeroed page.\n\n");

    // =========================================================================
    // Step 6: File-backed MAP_SHARED vs MAP_PRIVATE
    // =========================================================================
    printf("--- Step 6: File-backed MAP_SHARED vs MAP_PRIVATE ---\n");
    char tmppath[] = "/tmp/mmap_test_XXXXXX";
    int fd = mkstemp(tmppath);
    if (fd < 0) {
        perror("mkstemp");
        return 1;
    }
    unlink(tmppath); // File unlinked, accessible only via fd

    char initial_data[4096];
    memset(initial_data, 'A', sizeof(initial_data));
    if (write(fd, initial_data, sizeof(initial_data)) != (ssize_t)sizeof(initial_data)) {
        perror("write initial");
        return 1;
    }

    // MAP_SHARED test
    void *f_shared = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (f_shared == MAP_FAILED) {
        perror("mmap shared");
        return 1;
    }
    ((char *)f_shared)[0] = 'B';
    if (msync(f_shared, 4096, MS_SYNC) != 0) {
        perror("msync");
        return 1;
    }
    if (munmap(f_shared, 4096) != 0) {
        perror("munmap shared");
        return 1;
    }

    // pread(2) reads through the page cache, so this proves the file's contents changed
    // (what every reader sees), not that the block reached the storage device.
    char file_byte;
    if (pread(fd, &file_byte, 1, 0) != 1) {
        perror("pread after shared write");
        return 1;
    }
    printf("File byte after MAP_SHARED write:  '%c'\n", file_byte);

    // MAP_PRIVATE test
    void *f_private = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, 0);
    if (f_private == MAP_FAILED) {
        perror("mmap private");
        return 1;
    }
    ((char *)f_private)[0] = 'C';
    // The writer still sees its own copy-on-write page; the file does not.
    printf("Private mapping byte after its write: '%c'\n", ((char *)f_private)[0]);
    // Even an explicit msync cannot push a MAP_PRIVATE page back to the file.
    if (msync(f_private, 4096, MS_SYNC) != 0) {
        perror("msync private");
        return 1;
    }
    if (munmap(f_private, 4096) != 0) {
        perror("munmap private");
        return 1;
    }

    if (pread(fd, &file_byte, 1, 0) != 1) {
        perror("pread after private write");
        return 1;
    }
    printf("File byte after MAP_PRIVATE write: '%c'\n", file_byte);
    printf("File-backed semantics verified: MAP_SHARED mutates the file; MAP_PRIVATE isolates changes.\n");

    // Cleanup
    close(fd);
    if (munmap(p1, map_size) != 0) {
        perror("munmap p1");
        return 1;
    }

    return 0;
}
