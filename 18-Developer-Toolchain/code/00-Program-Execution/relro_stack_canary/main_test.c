#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
#include <link.h>
#include <elf.h>

extern void target_func(void);
extern int *target_data_ptr;

void hijacked_func(void) {
    printf("Hijacked target_func execution!\n");
}

struct search_ctx {
    const char *name;
    void *slot;
    int is_copy;
};

// A copy relocation puts the variable itself in .bss, so the slot is not a GOT entry.
static int is_copy_reloc(uint32_t type) {
#if defined(__aarch64__)
    return type == R_AARCH64_COPY;
#elif defined(__x86_64__)
    return type == R_X86_64_COPY;
#else
    return 0;
#endif
}

static int phdr_callback(struct dl_phdr_info *info, size_t size, void *arg) {
    struct search_ctx *s = (struct search_ctx *)arg;
    if (info->dlpi_name != NULL && strlen(info->dlpi_name) > 0) return 0;

    ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        if (info->dlpi_phdr[i].p_type == PT_DYNAMIC) {
            dyn = (ElfW(Dyn) *)(info->dlpi_addr + info->dlpi_phdr[i].p_vaddr);
            break;
        }
    }
    if (!dyn) return 0;

    ElfW(Rela) *rela_plt = NULL, *rela_dyn = NULL;
    size_t rela_plt_sz = 0, rela_dyn_sz = 0;
    ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;

    for (ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_JMPREL) rela_plt = (ElfW(Rela) *)d->d_un.d_ptr;
        else if (d->d_tag == DT_PLTRELSZ) rela_plt_sz = d->d_un.d_val;
        else if (d->d_tag == DT_RELA) rela_dyn = (ElfW(Rela) *)d->d_un.d_ptr;
        else if (d->d_tag == DT_RELASZ) rela_dyn_sz = d->d_un.d_val;
        else if (d->d_tag == DT_SYMTAB) symtab = (ElfW(Sym) *)d->d_un.d_ptr;
        else if (d->d_tag == DT_STRTAB) strtab = (const char *)d->d_un.d_ptr;
    }

    if (!symtab || !strtab) return 0;

    if (rela_plt) {
        size_t count = rela_plt_sz / sizeof(ElfW(Rela));
        for (size_t i = 0; i < count; i++) {
            uint32_t sym_idx = ELF64_R_SYM(rela_plt[i].r_info);
            if (strcmp(strtab + symtab[sym_idx].st_name, s->name) == 0) {
                s->slot = (void *)(info->dlpi_addr + rela_plt[i].r_offset);
                return 1;
            }
        }
    }

    if (rela_dyn) {
        size_t count = rela_dyn_sz / sizeof(ElfW(Rela));
        for (size_t i = 0; i < count; i++) {
            uint32_t sym_idx = ELF64_R_SYM(rela_dyn[i].r_info);
            if (strcmp(strtab + symtab[sym_idx].st_name, s->name) == 0) {
                s->slot = (void *)(info->dlpi_addr + rela_dyn[i].r_offset);
                s->is_copy = is_copy_reloc(ELF64_R_TYPE(rela_dyn[i].r_info));
                return 1;
            }
        }
    }
    return 0;
}

static const char *signame(int sig) {
    return sig == SIGABRT ? "SIGABRT" : sig == SIGSEGV ? "SIGSEGV" : "OTHER";
}

static void *find_slot(const char *name, int *is_copy) {
    struct search_ctx ctx = { name, NULL, 0 };
    dl_iterate_phdr(phdr_callback, &ctx);
    if (is_copy) *is_copy = ctx.is_copy;
    return ctx.slot;
}

__attribute__((noinline))
static void test_canary_overflow(int do_overflow) {
    volatile char buf[16];
    if (do_overflow) {
        memset((void *)buf, 'X', 64);
    } else {
        memset((void *)buf, 'A', 15);
        buf[15] = '\0';
    }
}

int main(int argc, char **argv) {
    // Force reference to target_data_ptr so dynamic relocation is emitted
    volatile void *dummy_ref = target_data_ptr;
    (void)dummy_ref;

    if (argc < 2) {
        fprintf(stderr, "Usage: %s <mode: canary-safe|canary-smash|stack-exec|relro-func|relro-data>\n", argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "canary-safe") == 0) {
        test_canary_overflow(0);
        printf("returned normally\n");
        return 0;
    }

    if (strcmp(argv[1], "canary-smash") == 0) {
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            close(2); // suppress verbose crash text
            test_canary_overflow(1);
            exit(0);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        if (WIFSIGNALED(status)) {
            printf("terminated with signal %d (%s)\n", WTERMSIG(status), signame(WTERMSIG(status)));
        } else {
            printf("exited with status %d (no signal)\n", WEXITSTATUS(status));
        }
        return 0;
    }

    if (strcmp(argv[1], "stack-exec") == 0) {
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            // Copy a bare "ret" instruction into a stack buffer and call it.
#if defined(__aarch64__)
            static const unsigned char ret_insn[4] = {0xc0, 0x03, 0x5f, 0xd6};
#elif defined(__x86_64__)
            static const unsigned char ret_insn[1] = {0xc3};
#else
#error "stack-exec needs an AArch64 or x86_64 ret encoding"
#endif
            unsigned char code[16] __attribute__((aligned(16)));
            memcpy(code, ret_insn, sizeof ret_insn);
            __builtin___clear_cache((char *)code, (char *)code + sizeof code);
            ((void (*)(void))code)();
            exit(0);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        if (WIFSIGNALED(status)) {
            printf("code on the stack: caught signal %d (%s)\n", WTERMSIG(status), signame(WTERMSIG(status)));
        } else {
            printf("code on the stack: executed\n");
        }
        return 0;
    }

    if (strcmp(argv[1], "relro-func") == 0) {
        target_func();

        void *slot = find_slot("target_func", NULL);
        if (!slot) {
            printf("GOT slot for target_func: not found\n");
            return 1;
        }

        fflush(stdout);

        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            uintptr_t *ptr = (uintptr_t *)slot;
            *ptr = (uintptr_t)hijacked_func;
            target_func();
            exit(0);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        if (WIFSIGNALED(status)) {
            printf("GOT write target_func: caught signal %d (%s)\n",
                   WTERMSIG(status),
                   signame(WTERMSIG(status)));
        } else {
            printf("GOT write target_func: succeeded (writable GOT)\n");
        }
        return 0;
    }

    if (strcmp(argv[1], "relro-data") == 0) {
        int is_copy = 0;
        void *slot = find_slot("target_data_ptr", &is_copy);
        if (!slot) {
            printf("GOT slot for target_data_ptr: not found\n");
            return 1;
        }
        if (is_copy) {
            printf("no GOT slot for target_data_ptr: the linker used a copy relocation into .bss\n");
            return 0;
        }

        fflush(stdout);

        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            uintptr_t *ptr = (uintptr_t *)slot;
            *ptr = 0x12345678;
            exit(0);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        if (WIFSIGNALED(status)) {
            printf("GOT write target_data_ptr: caught signal %d (%s)\n",
                   WTERMSIG(status),
                   signame(WTERMSIG(status)));
        } else {
            printf("GOT write target_data_ptr: succeeded (writable GOT)\n");
        }
        return 0;
    }

    return 0;
}
