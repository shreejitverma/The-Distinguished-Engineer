#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/fsuid.h>
#include <sys/auxv.h>
#include <elf.h>
#include <stdint.h>

static void test_maps_and_smaps_rollup(void) {
    size_t page_sz = sysconf(_SC_PAGESIZE);
    size_t alloc_sz = page_sz * 4;
    char *p = mmap(NULL, alloc_sz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) {
        perror("mmap");
        exit(1);
    }
    /* Touch first 2 pages */
    memset(p, 0x42, page_sz * 2);

    /* maps: the VMA that contains the mmap pointer must be private, writable, anonymous (inode 0) */
    FILE *fmaps = fopen("/proc/self/maps", "r");
    if (!fmaps) {
        perror("fopen maps");
        exit(1);
    }
    char line[512];
    int found_anon = 0;
    while (fgets(line, sizeof(line), fmaps)) {
        unsigned long lo, hi, off, inode;
        unsigned int dev_major, dev_minor;
        char perms[8];
        if (sscanf(line, "%lx-%lx %7s %lx %x:%x %lu", &lo, &hi, perms, &off, &dev_major, &dev_minor, &inode) != 7)
            continue;
        if ((uintptr_t)p >= lo && (uintptr_t)(p + alloc_sz) <= hi) {
            found_anon = (strcmp(perms, "rw-p") == 0 && inode == 0);
            break;
        }
    }
    fclose(fmaps);

    /* smaps_rollup: parse the kB values; the two touched pages must be counted in Rss and Anonymous */
    FILE *frollup = fopen("/proc/self/smaps_rollup", "r");
    int has_rss = 0, has_pss = 0, has_anon = 0;
    if (frollup) {
        unsigned long kb;
        unsigned long touched_kb = (page_sz * 2) / 1024;
        while (fgets(line, sizeof(line), frollup)) {
            if (sscanf(line, "Rss: %lu kB", &kb) == 1 && kb >= touched_kb) has_rss = 1;
            if (sscanf(line, "Pss: %lu kB", &kb) == 1 && kb > 0) has_pss = 1;
            if (sscanf(line, "Anonymous: %lu kB", &kb) == 1 && kb >= touched_kb) has_anon = 1;
        }
        fclose(frollup);
    }

    printf("[1] maps_and_smaps_rollup: found_anon_vma=%d has_rss=%d has_pss=%d has_anon=%d\n",
           found_anon, has_rss, has_pss, has_anon);
    munmap(p, alloc_sz);
}

static void test_pagemap(void) {
    size_t page_sz = sysconf(_SC_PAGESIZE);
    char *p = mmap(NULL, page_sz * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) {
        perror("mmap");
        exit(1);
    }
    /* Touch page 0 only */
    p[0] = 'A';

    int fd = open("/proc/self/pagemap", O_RDONLY);
    if (fd < 0) {
        perror("open pagemap");
        exit(1);
    }

    uint64_t entry0 = 0, entry1 = 0;
    uintptr_t vaddr0 = (uintptr_t)p;
    uintptr_t vaddr1 = (uintptr_t)(p + page_sz);

    off_t offset0 = (vaddr0 / page_sz) * sizeof(uint64_t);
    off_t offset1 = (vaddr1 / page_sz) * sizeof(uint64_t);

    if (pread(fd, &entry0, sizeof(entry0), offset0) != sizeof(entry0) ||
        pread(fd, &entry1, sizeof(entry1), offset1) != sizeof(entry1)) {
        perror("pread pagemap");
        exit(1);
    }
    close(fd);

    int page0_present = (entry0 >> 63) & 1;
    int page0_exclusive = (entry0 >> 56) & 1;
    uint64_t page0_pfn = entry0 & 0x7fffffffffffffULL; /* bits 0-54 */

    int page1_present = (entry1 >> 63) & 1;

    printf("[2] pagemap: touched_present=%d touched_exclusive=%d unpriv_pfn_zeroed=%d untouched_present=%d\n",
           page0_present, page0_exclusive, (page0_pfn == 0), page1_present);

    munmap(p, page_sz * 2);
}

static void test_fd_and_fdinfo(void) {
    int pipefds[2];
    if (pipe2(pipefds, O_CLOEXEC) < 0) {
        perror("pipe2");
        exit(1);
    }

    char linkpath[64];
    char target[128];
    snprintf(linkpath, sizeof(linkpath), "/proc/self/fd/%d", pipefds[0]);
    ssize_t len = readlink(linkpath, target, sizeof(target) - 1);
    if (len < 0) {
        perror("readlink fd");
        exit(1);
    }
    target[len] = '\0';
    int is_pipe = (strncmp(target, "pipe:[", 6) == 0);

    char infopath[64];
    snprintf(infopath, sizeof(infopath), "/proc/self/fdinfo/%d", pipefds[0]);
    FILE *finfo = fopen(infopath, "r");
    int has_pos = 0, has_flags = 0, cloexec_flag_set = 0;
    if (finfo) {
        char line[128];
        while (fgets(line, sizeof(line), finfo)) {
            if (strncmp(line, "pos:\t0", 6) == 0) has_pos = 1;
            if (strncmp(line, "flags:\t", 7) == 0) {
                has_flags = 1;
                /* Octal flags: O_CLOEXEC is octal 02000000 */
                long flags = strtol(line + 7, NULL, 8);
                if (flags & O_CLOEXEC) cloexec_flag_set = 1;
            }
        }
        fclose(finfo);
    }
    close(pipefds[0]);
    close(pipefds[1]);

    printf("[3] fd_and_fdinfo: is_pipe_symlink=%d has_pos_zero=%d has_flags=%d cloexec_in_fdinfo=%d\n",
           is_pipe, has_pos, has_flags, cloexec_flag_set);
}

static void test_auxv(void) {
    int fd = open("/proc/self/auxv", O_RDONLY);
    if (fd < 0) {
        perror("open auxv");
        exit(1);
    }

    unsigned long at_pagesz_val = 0;
    int found_pagesz = 0;
    int found_secure = 0;
    unsigned long at_secure_val = 999;

    Elf64_auxv_t entry;
    while (read(fd, &entry, sizeof(entry)) == sizeof(entry)) {
        if (entry.a_type == AT_NULL) break;
        if (entry.a_type == AT_PAGESZ) {
            found_pagesz = 1;
            at_pagesz_val = entry.a_un.a_val;
        }
        if (entry.a_type == AT_SECURE) {
            found_secure = 1;
            at_secure_val = entry.a_un.a_val;
        }
    }
    close(fd);

    unsigned long getaux_pagesz = getauxval(AT_PAGESZ);
    int pagesz_match = (found_pagesz && at_pagesz_val == getaux_pagesz && at_pagesz_val == (unsigned long)sysconf(_SC_PAGESIZE));

    printf("[4] auxv: found_pagesz=%d pagesz_match=%d found_secure=%d at_secure_val=%lu\n",
           found_pagesz, pagesz_match, found_secure, at_secure_val);
}

static void test_status_credentials_and_caps(void) {
    uid_t ruid, euid, suid;
    if (getresuid(&ruid, &euid, &suid) < 0) {
        perror("getresuid");
        exit(1);
    }

    /* Valid credential transition: setting current IDs succeeds */
    int setres_ok = (setresuid(ruid, euid, suid) == 0);

    /* setfsuid verification idiom: the first call returns the previous fsuid, the second (-1) reads the current one */
    int prev_fsuid = setfsuid(euid);
    int curr_fsuid = setfsuid(-1);
    int fsuid_matches_euid = (prev_fsuid == (int)euid && curr_fsuid == (int)euid);

    /* A transition the lab user is not allowed to make (no CAP_SETUID, id not in real/effective/saved/fs):
     * setfsuid() returns the previous value and sets no errno, so only the -1 read-back reveals the denial. */
    uid_t other_uid = euid + 1;
    int prev_denied = setfsuid(other_uid);
    int after_denied = setfsuid(-1);
    int fsuid_denied_detected = (prev_denied == (int)euid && after_denied != (int)other_uid && after_denied == (int)euid);

    /* Parse /proc/self/status: Uid must equal getresuid plus the fsuid, CapEff must be a hex mask, NoNewPrivs 0 or 1 */
    FILE *f = fopen("/proc/self/status", "r");
    if (!f) {
        perror("fopen status");
        exit(1);
    }
    char line[256];
    int parsed_uid = 0, parsed_caps = 0, parsed_nnp = 0;
    while (fgets(line, sizeof(line), f)) {
        unsigned int u_r, u_e, u_s, u_f, nnp;
        unsigned long long cap;
        if (sscanf(line, "Uid: %u %u %u %u", &u_r, &u_e, &u_s, &u_f) == 4)
            parsed_uid = (u_r == ruid && u_e == euid && u_s == suid && u_f == (unsigned int)after_denied);
        if (sscanf(line, "CapEff: %llx", &cap) == 1) parsed_caps = 1;
        if (sscanf(line, "NoNewPrivs: %u", &nnp) == 1 && nnp <= 1) parsed_nnp = 1;
    }
    fclose(f);

    printf("[5] status_and_credentials: setresuid_ok=%d fsuid_matches_euid=%d fsuid_denied_detected=%d parsed_uid=%d parsed_caps=%d parsed_nnp=%d\n",
           setres_ok, fsuid_matches_euid, fsuid_denied_detected, parsed_uid, parsed_caps, parsed_nnp);
}

static void test_oom_score_adj(void) {
    FILE *f = fopen("/proc/self/oom_score_adj", "r");
    if (!f) {
        perror("fopen oom_score_adj");
        exit(1);
    }
    int initial_score = 0;
    if (fscanf(f, "%d", &initial_score) != 1) {
        perror("fscanf oom_score_adj");
        exit(1);
    }
    fclose(f);

    /* Adjust upward to +150 */
    int write_up_ok = 0;
    f = fopen("/proc/self/oom_score_adj", "w");
    if (f) {
        if (fprintf(f, "150\n") > 0) {
            fflush(f);
            write_up_ok = 1;
        }
        fclose(f);
    }

    /* Read back */
    int new_score = 0;
    f = fopen("/proc/self/oom_score_adj", "r");
    if (f) {
        if (fscanf(f, "%d", &new_score) == 1) {}
        fclose(f);
    }

    /* Try downward write below 0 (requires CAP_SYS_RESOURCE) */
    int downward_errno = 0;
    int downward_fd = open("/proc/self/oom_score_adj", O_WRONLY);
    if (downward_fd >= 0) {
        if (write(downward_fd, "-50\n", 4) < 0) {
            downward_errno = errno;
        }
        close(downward_fd);
    }

    /* Restore to initial score */
    f = fopen("/proc/self/oom_score_adj", "w");
    if (f) {
        fprintf(f, "%d\n", initial_score);
        fclose(f);
    }

    printf("[6] oom_score_adj: initial=%d write_up_ok=%d new_score=%d downward_errno=%d (EACCES=%d)\n",
           initial_score, write_up_ok, new_score, downward_errno, EACCES);
}

static void test_dumpable(void) {
    int orig_dumpable = prctl(PR_GET_DUMPABLE, 0, 0, 0, 0);
    int set_zero_ok = (prctl(PR_SET_DUMPABLE, 0L, 0, 0, 0) == 0);
    int queried_zero = prctl(PR_GET_DUMPABLE, 0, 0, 0, 0);
    int set_orig_ok = (prctl(PR_SET_DUMPABLE, (long)orig_dumpable, 0, 0, 0) == 0);
    int restored = prctl(PR_GET_DUMPABLE, 0, 0, 0, 0);

    printf("[7] dumpable: orig=%d set_zero_ok=%d queried_zero=%d set_orig_ok=%d restored=%d\n",
           orig_dumpable, set_zero_ok, queried_zero, set_orig_ok, restored);
}

int main(void) {
    test_maps_and_smaps_rollup();
    test_pagemap();
    test_fd_and_fdinfo();
    test_auxv();
    test_status_credentials_and_caps();
    test_oom_score_adj();
    test_dumpable();
    return 0;
}
