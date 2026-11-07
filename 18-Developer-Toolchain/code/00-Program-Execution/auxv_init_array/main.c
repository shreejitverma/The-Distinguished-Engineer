#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdint.h>
#include <stdbool.h>
#include <fcntl.h>
#include <elf.h>
#include <sys/auxv.h>
#include <pthread.h>

extern char _start[]; /* the executable entry symbol from crt1.o */

extern void lib_marker(void);

__thread int g_tls_canary = 0x1337;

static void atexit_handler_ctor(void) {
    printf("[atexit] registered during exe constructor\n");
    fflush(stdout);
}

static void atexit_handler_main(void) {
    printf("[atexit] registered during main\n");
    fflush(stdout);
}

__attribute__((constructor(101)))
static void exe_ctor_101(void) {
    atexit(atexit_handler_ctor);
    printf("[exe] constructor priority 101 (TLS canary: 0x%x)\n", g_tls_canary);
    g_tls_canary += 1;
    fflush(stdout);
}

__attribute__((constructor(200)))
static void exe_ctor_200(void) {
    printf("[exe] constructor priority 200 (TLS canary: 0x%x)\n", g_tls_canary);
    g_tls_canary += 1;
    fflush(stdout);
}

__attribute__((constructor))
static void exe_ctor_default(void) {
    printf("[exe] constructor priority default (TLS canary: 0x%x)\n", g_tls_canary);
    g_tls_canary += 1;
    fflush(stdout);
}

__attribute__((destructor(101)))
static void exe_dtor_101(void) {
    printf("[exe] destructor priority 101\n");
    fflush(stdout);
}

__attribute__((destructor(200)))
static void exe_dtor_200(void) {
    printf("[exe] destructor priority 200\n");
    fflush(stdout);
}

__attribute__((destructor))
static void exe_dtor_default(void) {
    printf("[exe] destructor priority default\n");
    fflush(stdout);
}

static bool verify_elf_magic(const void *addr) {
    if (!addr) return false;
    const unsigned char *p = (const unsigned char *)addr;
    return (p[0] == 0x7f && p[1] == 'E' && p[2] == 'L' && p[3] == 'F');
}

static void *tls_probe(void *out) {
    *(int *)out = g_tls_canary;
    return NULL;
}

/* Read the raw (type, value) records of /proc/self/auxv into buf; returns the record count. */
static size_t read_proc_auxv(Elf64_auxv_t *buf, size_t cap) {
    int fd = open("/proc/self/auxv", O_RDONLY);
    size_t n = 0;
    if (fd < 0) return 0;
    while (n < cap && read(fd, &buf[n], sizeof(buf[n])) == (ssize_t)sizeof(buf[n])) {
        if (buf[n].a_type == AT_NULL) break;
        n++;
    }
    close(fd);
    return n;
}

static bool proc_auxv_find(const Elf64_auxv_t *buf, size_t n, unsigned long type, unsigned long *val) {
    for (size_t i = 0; i < n; i++) {
        if (buf[i].a_type == type) {
            *val = buf[i].a_un.a_val;
            return true;
        }
    }
    return false;
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    lib_marker(); /* reference symbol from libdep.so */

    atexit(atexit_handler_main);
    printf("[main] entry reached (TLS canary: 0x%x)\n", g_tls_canary);

    /* A second thread gets a fresh copy of the TLS image, so g_tls_canary is really thread-local. */
    int other_thread_value = -1;
    pthread_t th;
    if (pthread_create(&th, NULL, tls_probe, &other_thread_value) == 0) pthread_join(th, NULL);
    printf("[main] new thread sees initial TLS image: 0x%x (main thread: 0x%x)\n",
           other_thread_value, g_tls_canary);

    /* 1. Validate AT_PAGESZ */
    unsigned long pagesz = getauxval(AT_PAGESZ);
    long sc_pagesz = sysconf(_SC_PAGESIZE);
    printf("auxv AT_PAGESZ matches sysconf: %s (non-zero: %s)\n",
           (pagesz == (unsigned long)sc_pagesz) ? "true" : "false",
           (pagesz > 0) ? "true" : "false");

    /* 2. Validate AT_ENTRY: it must be the address of the executable's _start */
    unsigned long entry = getauxval(AT_ENTRY);
    printf("auxv AT_ENTRY equals &_start: %s\n", (entry == (unsigned long)_start) ? "true" : "false");

    /* 3. Validate AT_BASE (interpreter ld.so) */
    unsigned long base = getauxval(AT_BASE);
    bool base_is_elf = verify_elf_magic((const void *)base);
    bool base_is_dyn = base_is_elf && ((const Elf64_Ehdr *)base)->e_type == ET_DYN;
    printf("auxv AT_BASE non-zero: %s (ELF header valid: %s, e_type is ET_DYN: %s)\n",
           (base != 0) ? "true" : "false", base_is_elf ? "true" : "false", base_is_dyn ? "true" : "false");

    /* 4. Validate AT_PHDR and AT_PHNUM */
    unsigned long phdr = getauxval(AT_PHDR);
    unsigned long phnum = getauxval(AT_PHNUM);
    unsigned long phent = getauxval(AT_PHENT);
    printf("auxv AT_PHDR valid: %s (phnum > 0: %s, phent == sizeof(Elf64_Phdr): %s)\n",
           (phdr != 0) ? "true" : "false",
           (phnum > 0) ? "true" : "false",
           (phent == sizeof(Elf64_Phdr)) ? "true" : "false");

    /* Inspect program header types */
    bool has_load = false;
    bool has_interp = false;
    bool has_dynamic = false;
    bool has_tls = false;
    if (phdr != 0 && phent == sizeof(Elf64_Phdr)) {
        const Elf64_Phdr *ph = (const Elf64_Phdr *)phdr;
        for (unsigned long i = 0; i < phnum; i++) {
            if (ph[i].p_type == PT_LOAD) has_load = true;
            if (ph[i].p_type == PT_INTERP) has_interp = true;
            if (ph[i].p_type == PT_DYNAMIC) has_dynamic = true;
            if (ph[i].p_type == PT_TLS) has_tls = true;
        }
    }
    printf("auxv program headers contain LOAD: %s, INTERP: %s, DYNAMIC: %s, TLS: %s\n",
           has_load ? "true" : "false",
           has_interp ? "true" : "false",
           has_dynamic ? "true" : "false",
           has_tls ? "true" : "false");

    /* 5. Validate AT_RANDOM: 16 readable bytes holding at least 8 distinct values
          (P(fewer than 8 distinct in 16 random bytes) is about 6e-13).
          glibc 2.44 reseeds these bytes with getrandom, so they stay random on any glibc. */
    const uint8_t *rand_bytes = (const uint8_t *)getauxval(AT_RANDOM);
    int distinct = 0;
    if (rand_bytes) {
        bool seen[256] = {false};
        for (int i = 0; i < 16; i++) {
            if (!seen[rand_bytes[i]]) {
                seen[rand_bytes[i]] = true;
                distinct++;
            }
        }
    }
    printf("auxv AT_RANDOM has at least 8 distinct bytes of 16: %s\n", distinct >= 8 ? "true" : "false");

    /* 6. Validate AT_SYSINFO_EHDR (vDSO) */
    unsigned long vdso = getauxval(AT_SYSINFO_EHDR);
    if (vdso != 0) {
        bool vdso_is_elf = verify_elf_magic((const void *)vdso);
        printf("auxv AT_SYSINFO_EHDR (vDSO) valid: %s (ELF header valid: %s)\n",
               "true", vdso_is_elf ? "true" : "false");
    } else {
        printf("auxv AT_SYSINFO_EHDR (vDSO) valid: false\n");
    }

    /* 7. Cross-check getauxval against the raw /proc/self/auxv records, type by type.
          AT_SECURE needs this: glibc answers getauxval(AT_SECURE) from its own flag, so only the
          kernel record shows that the kernel supplied it and what it said. */
    static const unsigned long types[] = {
        AT_PAGESZ, AT_ENTRY, AT_BASE, AT_PHDR, AT_PHENT, AT_PHNUM, AT_RANDOM, AT_SYSINFO_EHDR, AT_SECURE,
    };
    const size_t ntypes = sizeof(types) / sizeof(types[0]);
    static Elf64_auxv_t records[128];
    size_t nrec = read_proc_auxv(records, sizeof(records) / sizeof(records[0]));
    size_t matched = 0;
    for (size_t i = 0; i < ntypes; i++) {
        unsigned long v = 0;
        if (proc_auxv_find(records, nrec, types[i], &v) && v == getauxval(types[i])) matched++;
    }
    printf("/proc/self/auxv matches getauxval for all %zu checked types: %s\n",
           ntypes, (matched == ntypes) ? "true" : "false");

    unsigned long proc_secure = 99;
    bool secure_found = proc_auxv_find(records, nrec, AT_SECURE, &proc_secure);
    printf("auxv AT_SECURE record present in /proc/self/auxv: %s (value: %lu)\n",
           secure_found ? "true" : "false", secure_found ? proc_secure : 0UL);

    printf("[main] exiting\n");
    fflush(stdout);
    return 0;
}
