#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <link.h>
#include <elf.h>
#include <stdint.h>

#if __ELF_NATIVE_CLASS != 64
#error "this lab reads ElfW(Rela) entries and uses ELF64_R_SYM, so it needs an ELFCLASS64 target"
#endif

extern int compute(int a, int b);

static uintptr_t *got_slot = NULL;
static uintptr_t base_addr = 0;
/* Address range covered by the executable's own PT_LOAD segments (its .plt lives inside it). */
static uintptr_t exe_lo = UINTPTR_MAX;
static uintptr_t exe_hi = 0;

static int phdr_callback(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    (void)data;
    if (info->dlpi_name != NULL && info->dlpi_name[0] != '\0') {
        return 0;
    }
    base_addr = info->dlpi_addr;

    ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        if (info->dlpi_phdr[i].p_type == PT_LOAD) {
            uintptr_t lo = base_addr + info->dlpi_phdr[i].p_vaddr;
            uintptr_t hi = lo + info->dlpi_phdr[i].p_memsz;
            if (lo < exe_lo) exe_lo = lo;
            if (hi > exe_hi) exe_hi = hi;
        } else if (info->dlpi_phdr[i].p_type == PT_DYNAMIC) {
            dyn = (ElfW(Dyn) *)(base_addr + info->dlpi_phdr[i].p_vaddr);
        }
    }
    if (!dyn) return 1;

    ElfW(Rela) *jmprel = NULL;
    size_t pltrelsz = 0;
    ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;

    for (ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_JMPREL) {
            jmprel = (ElfW(Rela) *)d->d_un.d_ptr;
        } else if (d->d_tag == DT_PLTRELSZ) {
            pltrelsz = d->d_un.d_val;
        } else if (d->d_tag == DT_SYMTAB) {
            symtab = (ElfW(Sym) *)d->d_un.d_ptr;
        } else if (d->d_tag == DT_STRTAB) {
            strtab = (const char *)d->d_un.d_ptr;
        }
    }

    if (jmprel && symtab && strtab && pltrelsz > 0) {
        size_t nrel = pltrelsz / sizeof(ElfW(Rela));
        for (size_t i = 0; i < nrel; i++) {
            ElfW(Word) sym_idx = ELF64_R_SYM(jmprel[i].r_info);
            const char *name = strtab + symtab[sym_idx].st_name;
            if (strcmp(name, "compute") == 0) {
                got_slot = (uintptr_t *)(base_addr + jmprel[i].r_offset);
                break;
            }
        }
    }
    return 1;
}

/* Say where an address lives: in the executable (where the PLT stub lives), or in which shared object. */
static void describe(uintptr_t addr, char *out, size_t n) {
    if (addr >= exe_lo && addr < exe_hi) {
        snprintf(out, n, "the executable");
        return;
    }
    Dl_info info;
    if (!dladdr((void *)addr, &info) || !info.dli_fname) {
        snprintf(out, n, "unknown");
        return;
    }
    const char *base = strrchr(info.dli_fname, '/');
    base = base ? base + 1 : info.dli_fname;
    snprintf(out, n, "%s, symbol %s", base, info.dli_sname ? info.dli_sname : "(none)");
}

int main(void) {
    dl_iterate_phdr(phdr_callback, NULL);
    if (!got_slot) {
        fprintf(stderr, "Failed to find GOT slot for compute\n");
        return 1;
    }

    char where[256];
    printf("GOT slot address located: valid\n");
    describe(*got_slot, where, sizeof where);
    printf("Before first call: GOT points into %s\n", where);

    int c = compute(10, 20);
    printf("First call result: %d\n", c);

    describe(*got_slot, where, sizeof where);
    printf("After first call: GOT points into %s\n", where);

    int c2 = compute(15, 25);
    printf("Second call result: %d\n", c2);

    return 0;
}
