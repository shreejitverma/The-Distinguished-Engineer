#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__x86_64__)
#define ARCH_NAME "x86_64"
#define BP_SIZE 1
#define BP_OPCODE 0xCC
#elif defined(__aarch64__)
#include <asm/ptrace.h>
#define ARCH_NAME "aarch64"
#define BP_SIZE 4
#define BP_OPCODE 0xd4200000
#else
#define ARCH_NAME "unknown"
#endif

/* DWARF line table entry */
typedef struct {
    uint64_t address;
    uint32_t file;
    uint32_t line;
    uint32_t column;
    uint8_t is_stmt;
    uint8_t basic_block;
    uint8_t end_sequence;
    uint8_t prologue_end;
} LineEntry;

typedef struct {
    char name[256];
    uint32_t dir_idx;
} FileEntry;

typedef struct {
    LineEntry *entries;
    size_t count;
    size_t capacity;
    FileEntry *files;
    size_t file_count;
    char **dirs;
    size_t dir_count;
} LineTable;

static uint64_t read_uleb128(const uint8_t **p) {
    uint64_t res = 0;
    int shift = 0;
    while (1) {
        uint8_t byte = *(*p)++;
        res |= ((uint64_t)(byte & 0x7f)) << shift;
        if ((byte & 0x80) == 0) break;
        shift += 7;
    }
    return res;
}

static int64_t read_sleb128(const uint8_t **p) {
    int64_t res = 0;
    int shift = 0;
    uint8_t byte;
    while (1) {
        byte = *(*p)++;
        res |= ((int64_t)(byte & 0x7f)) << shift;
        shift += 7;
        if ((byte & 0x80) == 0) break;
    }
    if ((shift < 64) && (byte & 0x40)) {
        res |= -((int64_t)1 << shift);
    }
    return res;
}

static void add_line_entry(LineTable *lt, LineEntry entry) {
    if (lt->count >= lt->capacity) {
        lt->capacity = lt->capacity ? lt->capacity * 2 : 256;
        lt->entries = realloc(lt->entries, lt->capacity * sizeof(LineEntry));
    }
    lt->entries[lt->count++] = entry;
}

static int parse_debug_line(const uint8_t *debug_line, size_t line_size,
                            const uint8_t *debug_line_str, size_t line_str_size,
                            LineTable *lt) {
    (void)debug_line_str;
    (void)line_str_size;
    const uint8_t *p = debug_line;
    const uint8_t *end = debug_line + line_size;

    while (p < end) {
        uint32_t unit_len = *(const uint32_t *)p;
        p += 4;
        if (unit_len == 0xffffffff) {
            fprintf(stderr, "64-bit DWARF not supported in toy parser\n");
            return -1;
        }
        if (unit_len == 0) break;
        const uint8_t *cu_end = p + unit_len;

        uint16_t version = *(const uint16_t *)p;
        p += 2;
        uint8_t address_size = 8;
        if (version >= 5) {
            address_size = *p++;
            p++; /* segment_selector_size */
        }
        (void)address_size;

        uint32_t header_len = *(const uint32_t *)p;
        p += 4;
        const uint8_t *header_end = p + header_len;

        uint8_t min_insn_len = *p++;
        uint8_t max_ops_per_insn = 1;
        if (version >= 4) {
            max_ops_per_insn = *p++;
        }
        (void)max_ops_per_insn;

        uint8_t default_is_stmt = *p++;
        int8_t line_base = (int8_t)*p++;
        uint8_t line_range = *p++;
        uint8_t opcode_base = *p++;

        const uint8_t *std_opcode_lengths = p;
        p += (opcode_base - 1);
        (void)std_opcode_lengths;

        if (version <= 4) {
            /* Directories */
            while (*p != 0) {
                p += strlen((const char *)p) + 1;
            }
            p++; /* Skip terminating 0 */

            /* File names */
            while (*p != 0) {
                const char *fname = (const char *)p;
                p += strlen(fname) + 1;
                uint64_t dir_idx = read_uleb128(&p);
                read_uleb128(&p); /* mod_time */
                read_uleb128(&p); /* file_len */

                lt->files = realloc(lt->files, (lt->file_count + 1) * sizeof(FileEntry));
                strncpy(lt->files[lt->file_count].name, fname, sizeof(lt->files[0].name) - 1);
                lt->files[lt->file_count].dir_idx = (uint32_t)dir_idx;
                lt->file_count++;
            }
            p++; /* Skip terminating 0 */
        } else {
            /* DWARF 5 directory and file format skip to header_end */
            p = header_end;
        }

        p = header_end;

        /* State machine registers */
        uint64_t address = 0;
        uint32_t file = 1;
        uint32_t line = 1;
        uint32_t column = 0;
        uint8_t is_stmt = default_is_stmt;
        uint8_t basic_block = 0;
        uint8_t end_sequence = 0;
        uint8_t prologue_end = 0;

        while (p < cu_end) {
            uint8_t op = *p++;
            if (op == 0) {
                /* Extended opcode */
                uint64_t len = read_uleb128(&p);
                const uint8_t *op_end = p + len;
                uint8_t ext_op = *p++;
                if (ext_op == 1) { /* DW_LNE_end_sequence */
                    end_sequence = 1;
                    LineEntry e = {address, file, line, column, is_stmt, basic_block, end_sequence, prologue_end};
                    add_line_entry(lt, e);
                    /* Reset state machine */
                    address = 0;
                    file = 1;
                    line = 1;
                    column = 0;
                    is_stmt = default_is_stmt;
                    basic_block = 0;
                    end_sequence = 0;
                    prologue_end = 0;
                } else if (ext_op == 2) { /* DW_LNE_set_address */
                    address = *(const uint64_t *)p;
                    p += 8;
                }
                p = op_end;
            } else if (op < opcode_base) {
                /* Standard opcode */
                switch (op) {
                    case 1: { /* DW_LNS_copy */
                        LineEntry e = {address, file, line, column, is_stmt, basic_block, end_sequence, prologue_end};
                        add_line_entry(lt, e);
                        basic_block = 0;
                        prologue_end = 0;
                        break;
                    }
                    case 2: { /* DW_LNS_advance_pc */
                        address += read_uleb128(&p) * min_insn_len;
                        break;
                    }
                    case 3: { /* DW_LNS_advance_line */
                        line += read_sleb128(&p);
                        break;
                    }
                    case 4: { /* DW_LNS_set_file */
                        file = (uint32_t)read_uleb128(&p);
                        break;
                    }
                    case 5: { /* DW_LNS_set_column */
                        column = (uint32_t)read_uleb128(&p);
                        break;
                    }
                    case 6: { /* DW_LNS_negate_stmt */
                        is_stmt = !is_stmt;
                        break;
                    }
                    case 7: { /* DW_LNS_set_basic_block */
                        basic_block = 1;
                        break;
                    }
                    case 8: { /* DW_LNS_const_add_pc */
                        uint8_t adj = 255 - opcode_base;
                        address += (adj / line_range) * min_insn_len;
                        break;
                    }
                    case 9: { /* DW_LNS_fixed_advance_pc */
                        uint16_t adv = *(const uint16_t *)p;
                        p += 2;
                        address += adv;
                        break;
                    }
                    case 10: { /* DW_LNS_set_prologue_end */
                        prologue_end = 1;
                        break;
                    }
                    default:
                        /* Skip standard opcode operands */
                        break;
                }
            } else {
                /* Special opcode */
                uint8_t adj = op - opcode_base;
                int8_t line_inc = line_base + (adj % line_range);
                uint64_t addr_inc = (adj / line_range) * min_insn_len;
                line += line_inc;
                address += addr_inc;
                LineEntry e = {address, file, line, column, is_stmt, basic_block, end_sequence, prologue_end};
                add_line_entry(lt, e);
                basic_block = 0;
                prologue_end = 0;
            }
        }
        p = cu_end;
    }
    return 0;
}

static int load_elf_dwarf(const char *path, LineTable *lt, int *is_pie) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;

    struct stat st;
    if (fstat(fd, &st) < 0) { close(fd); return -1; }

    uint8_t *buf = malloc(st.st_size);
    if (read(fd, buf, st.st_size) != (ssize_t)st.st_size) {
        free(buf);
        close(fd);
        return -1;
    }
    close(fd);

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)buf;
    if (memcmp(eh->e_ident, ELFMAG, SELFMAG) != 0 || eh->e_ident[EI_CLASS] != ELFCLASS64) {
        free(buf);
        return -1;
    }

    *is_pie = (eh->e_type == ET_DYN);

    const Elf64_Shdr *sections = (const Elf64_Shdr *)(buf + eh->e_shoff);
    const char *shstrtab = (const char *)(buf + sections[eh->e_shstrndx].sh_offset);

    const uint8_t *debug_line = NULL;
    size_t line_size = 0;
    const uint8_t *debug_line_str = NULL;
    size_t line_str_size = 0;

    for (int i = 0; i < eh->e_shnum; i++) {
        const char *name = shstrtab + sections[i].sh_name;
        if (strcmp(name, ".debug_line") == 0) {
            debug_line = buf + sections[i].sh_offset;
            line_size = sections[i].sh_size;
        } else if (strcmp(name, ".debug_line_str") == 0) {
            debug_line_str = buf + sections[i].sh_offset;
            line_str_size = sections[i].sh_size;
        }
    }

    int ret = 0;
    if (debug_line && line_size > 0) {
        ret = parse_debug_line(debug_line, line_size, debug_line_str, line_str_size, lt);
    } else {
        ret = -1;
    }

    free(buf);
    return ret;
}

static uint64_t get_target_load_base(pid_t pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/maps", pid);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    uint64_t base = 0;
    if (fscanf(f, "%lx-", &base) != 1) {
        base = 0;
    }
    fclose(f);
    return base;
}

static int find_address_by_line(const LineTable *lt, uint32_t target_line, uint64_t *out_addr) {
    for (size_t i = 0; i < lt->count; i++) {
        if (lt->entries[i].line == target_line && lt->entries[i].is_stmt) {
            *out_addr = lt->entries[i].address;
            return 0;
        }
    }
    /* Fallback to any line match */
    for (size_t i = 0; i < lt->count; i++) {
        if (lt->entries[i].line == target_line) {
            *out_addr = lt->entries[i].address;
            return 0;
        }
    }
    return -1;
}

static int find_line_by_address(const LineTable *lt, uint64_t addr, uint32_t *out_line) {
    for (size_t i = 0; i < lt->count; i++) {
        if (lt->entries[i].end_sequence) continue;
        if (i + 1 < lt->count && !lt->entries[i + 1].end_sequence) {
            if (addr >= lt->entries[i].address && addr < lt->entries[i + 1].address) {
                *out_line = lt->entries[i].line;
                return 0;
            }
        } else if (addr == lt->entries[i].address) {
            *out_line = lt->entries[i].line;
            return 0;
        }
    }
    return -1;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        printf("usage: %s <binary_path> [line_number]\n", argv[0]);
        return 1;
    }

    const char *binary_path = argv[1];
    uint32_t target_line = (argc >= 3) ? (uint32_t)atoi(argv[2]) : 7;

    LineTable lt = {0};
    int is_pie = 0;
    if (load_elf_dwarf(binary_path, &lt, &is_pie) < 0) {
        fprintf(stderr, "failed to parse DWARF from %s\n", binary_path);
        return 1;
    }

    uint64_t bp_dwarf_addr = 0;
    if (find_address_by_line(&lt, target_line, &bp_dwarf_addr) < 0) {
        fprintf(stderr, "could not find line %u in DWARF\n", target_line);
        return 1;
    }

    printf("=== Tracer Target Setup ===\n");
    printf("Target executable: %s\n", binary_path);
    printf("Architecture: %s\n", ARCH_NAME);
    printf("DWARF lines parsed: %zu\n", lt.count);
    printf("Target breakpoint line: %u\n", target_line);

    pid_t child = fork();
    if (child == 0) {
        if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) < 0) {
            perror("PTRACE_TRACEME");
            exit(1);
        }
        char *const child_argv[] = {(char *)binary_path, NULL};
        execv(binary_path, child_argv);
        perror("execv");
        exit(1);
    }

    int status = 0;
    waitpid(child, &status, 0);
    if (!WIFSTOPPED(status)) {
        fprintf(stderr, "child failed to start under ptrace\n");
        return 1;
    }

    if (ptrace(PTRACE_SETOPTIONS, child, NULL, (void *)(PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEEXIT)) < 0) {
        perror("PTRACE_SETOPTIONS");
    }

    uint64_t load_base = is_pie ? get_target_load_base(child) : 0;
    uint64_t bp_runtime_addr = load_base + bp_dwarf_addr;

    /* Read original instruction word */
    errno = 0;
    long orig_word = ptrace(PTRACE_PEEKTEXT, child, (void *)bp_runtime_addr, NULL);
    if (orig_word == -1 && errno != 0) {
        perror("PTRACE_PEEKTEXT");
        return 1;
    }

    /* Inject breakpoint opcode */
    long patched_word = orig_word;
#if defined(__x86_64__)
    patched_word = (orig_word & ~0xFFUL) | 0xCCUL;
#elif defined(__aarch64__)
    if ((bp_runtime_addr & 4) == 0) {
        patched_word = (orig_word & ~0xFFFFFFFFUL) | 0xd4200000UL;
    } else {
        patched_word = (orig_word & 0xFFFFFFFFUL) | (0xd4200000UL << 32);
    }
#endif

    if (ptrace(PTRACE_POKETEXT, child, (void *)bp_runtime_addr, (void *)patched_word) < 0) {
        perror("PTRACE_POKETEXT");
        return 1;
    }

    printf("Breakpoint armed at 0xADDR (offset +0x%lx)\n", (unsigned long)bp_dwarf_addr);

    /* Resume child */
    if (ptrace(PTRACE_CONT, child, NULL, NULL) < 0) {
        perror("PTRACE_CONT");
        return 1;
    }

    waitpid(child, &status, 0);
    if (!WIFSTOPPED(status) || WSTOPSIG(status) != SIGTRAP) {
        fprintf(stderr, "unexpected stop: status=0x%x\n", status);
        return 1;
    }

    /* Inspect registers with PTRACE_GETREGSET */
    struct iovec iov;
#if defined(__x86_64__)
    struct user_regs_struct regs;
    iov.iov_base = &regs;
    iov.iov_len = sizeof(regs);
    if (ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov) < 0) {
        perror("PTRACE_GETREGSET");
        return 1;
    }
    /* x86-64: INT3 leaves RIP pointing to bp_addr + 1 */
    regs.rip -= 1;
    ptrace(PTRACE_SETREGSET, child, (void *)NT_PRSTATUS, &iov);
    uint64_t current_pc = regs.rip;
#elif defined(__aarch64__)
    struct user_pt_regs regs;
    iov.iov_base = &regs;
    iov.iov_len = sizeof(regs);
    if (ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov) < 0) {
        perror("PTRACE_GETREGSET");
        return 1;
    }
    /* AArch64: PC points directly at BRK */
    uint64_t current_pc = regs.pc;
#else
    uint64_t current_pc = bp_runtime_addr;
#endif

    uint64_t pc_dwarf_offset = current_pc - load_base;
    uint32_t resolved_line = 0;
    find_line_by_address(&lt, pc_dwarf_offset, &resolved_line);

    printf("Breakpoint trap caught: SIGTRAP\n");
    printf("Resolved source line: %u (expected %u)\n", resolved_line, target_line);

    /* Step over breakpoint:
       1. Restore original instruction word.
       2. Single step child.
       3. Re-arm breakpoint.
       4. Continue child. */
    ptrace(PTRACE_POKETEXT, child, (void *)bp_runtime_addr, (void *)orig_word);
    if (ptrace(PTRACE_SINGLESTEP, child, NULL, NULL) < 0) {
        perror("PTRACE_SINGLESTEP");
        return 1;
    }
    waitpid(child, &status, 0);

    /* Re-arm breakpoint */
    ptrace(PTRACE_POKETEXT, child, (void *)bp_runtime_addr, (void *)patched_word);

    /* Continue child to completion */
    ptrace(PTRACE_CONT, child, NULL, NULL);
    waitpid(child, &status, 0);

    if (WIFEXITED(status)) {
        printf("Tracee exited normally with status %d\n", WEXITSTATUS(status));
    }

    printf("=== Trace Completed Successfully ===\n");
    return 0;
}
