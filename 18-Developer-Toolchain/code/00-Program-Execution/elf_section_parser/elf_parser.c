#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <elf.h>

#define ERR_OK 0
#define ERR_BAD_MAGIC 1
#define ERR_NOT_64BIT 2
#define ERR_NOT_LITTLE_ENDIAN 3
#define ERR_BAD_HEADER_SIZE 4
#define ERR_BAD_SHSTRNDX 5
#define ERR_CONGRUENCE_VIOLATION 6
#define ERR_TRUNCATED_FILE 7
#define ERR_IO 8
#define ERR_BAD_SHNAME 9
#define ERR_BAD_EXTENDED 10

static const char *get_segment_type_name(uint32_t p_type) {
    switch (p_type) {
        case PT_NULL:         return "NULL";
        case PT_LOAD:         return "LOAD";
        case PT_DYNAMIC:      return "DYNAMIC";
        case PT_INTERP:       return "INTERP";
        case PT_NOTE:         return "NOTE";
        case PT_SHLIB:        return "SHLIB";
        case PT_PHDR:         return "PHDR";
        case PT_TLS:          return "TLS";
        case PT_GNU_EH_FRAME: return "GNU_EH_FRAME";
        case PT_GNU_STACK:    return "GNU_STACK";
        case PT_GNU_RELRO:    return "GNU_RELRO";
        case PT_GNU_PROPERTY: return "GNU_PROPERTY";
        default:              return "OTHER";
    }
}

static const char *get_section_type_name(uint32_t sh_type) {
    switch (sh_type) {
        case SHT_NULL:          return "NULL";
        case SHT_PROGBITS:      return "PROGBITS";
        case SHT_SYMTAB:        return "SYMTAB";
        case SHT_STRTAB:        return "STRTAB";
        case SHT_RELA:          return "RELA";
        case SHT_HASH:          return "HASH";
        case SHT_DYNAMIC:       return "DYNAMIC";
        case SHT_NOTE:          return "NOTE";
        case SHT_NOBITS:        return "NOBITS";
        case SHT_REL:           return "REL";
        case SHT_SHLIB:         return "SHLIB";
        case SHT_DYNSYM:        return "DYNSYM";
        case SHT_INIT_ARRAY:    return "INIT_ARRAY";
        case SHT_FINI_ARRAY:    return "FINI_ARRAY";
        case SHT_PREINIT_ARRAY: return "PREINIT_ARRAY";
        case SHT_GROUP:         return "GROUP";
        case SHT_SYMTAB_SHNDX:  return "SYMTAB_SHNDX";
        case SHT_GNU_HASH:      return "GNU_HASH";
        case SHT_GNU_verdef:    return "GNU_VERDEF";
        case SHT_GNU_verneed:   return "GNU_VERNEED";
        case SHT_GNU_versym:    return "GNU_VERSYM";
        default:                return "OTHER";
    }
}

static void format_segment_flags(uint32_t p_flags, char *buf) {
    buf[0] = (p_flags & PF_R) ? 'R' : ' ';
    buf[1] = (p_flags & PF_W) ? 'W' : ' ';
    buf[2] = (p_flags & PF_X) ? 'E' : ' ';
    buf[3] = '\0';
}

static void format_section_flags(uint64_t sh_flags, char *buf) {
    int idx = 0;
    if (sh_flags & SHF_WRITE)            buf[idx++] = 'W';
    if (sh_flags & SHF_ALLOC)            buf[idx++] = 'A';
    if (sh_flags & SHF_EXECINSTR)        buf[idx++] = 'X';
    if (sh_flags & SHF_MERGE)            buf[idx++] = 'M';
    if (sh_flags & SHF_STRINGS)          buf[idx++] = 'S';
    if (sh_flags & SHF_INFO_LINK)        buf[idx++] = 'I';
    if (sh_flags & SHF_LINK_ORDER)       buf[idx++] = 'L';
    if (sh_flags & SHF_OS_NONCONFORMING) buf[idx++] = 'O';
    if (sh_flags & SHF_GROUP)            buf[idx++] = 'G';
    if (sh_flags & SHF_TLS)              buf[idx++] = 'T';
    if (sh_flags & SHF_COMPRESSED)       buf[idx++] = 'C';
    if (idx == 0)                        buf[idx++] = '-';
    buf[idx] = '\0';
}

/* Read one Elf64_Shdr at file offset off; the caller has bounds-checked it. */
static bool read_shdr(FILE *f, uint64_t off, Elf64_Shdr *out) {
    return fseek(f, (long)off, SEEK_SET) == 0 && fread(out, sizeof(*out), 1, f) == 1;
}

int parse_elf(const char *filename, bool quiet) {
    FILE *f = fopen(filename, "rb");
    if (!f) {
        if (!quiet) fprintf(stderr, "Failed to open file: %s\n", filename);
        return ERR_IO;
    }

    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return ERR_IO;
    }
    long file_size_long = ftell(f);
    if (file_size_long < 0) {
        fclose(f);
        return ERR_IO;
    }
    uint64_t file_size = (uint64_t)file_size_long;
    rewind(f);

    if (file_size < sizeof(Elf64_Ehdr)) {
        if (!quiet) printf("ERROR: truncated ELF header (file size %lu < %zu)\n", (unsigned long)file_size, sizeof(Elf64_Ehdr));
        fclose(f);
        return ERR_TRUNCATED_FILE;
    }

    Elf64_Ehdr ehdr;
    if (fread(&ehdr, sizeof(ehdr), 1, f) != 1) {
        fclose(f);
        return ERR_IO;
    }

    if (ehdr.e_ident[EI_MAG0] != ELFMAG0 ||
        ehdr.e_ident[EI_MAG1] != ELFMAG1 ||
        ehdr.e_ident[EI_MAG2] != ELFMAG2 ||
        ehdr.e_ident[EI_MAG3] != ELFMAG3) {
        if (!quiet) printf("ERROR: invalid ELF magic number\n");
        fclose(f);
        return ERR_BAD_MAGIC;
    }

    if (ehdr.e_ident[EI_CLASS] != ELFCLASS64) {
        if (!quiet) printf("ERROR: unsupported ELF class (expected ELFCLASS64)\n");
        fclose(f);
        return ERR_NOT_64BIT;
    }

    if (ehdr.e_ident[EI_DATA] != ELFDATA2LSB) {
        if (!quiet) printf("ERROR: unsupported byte order (expected little-endian)\n");
        fclose(f);
        return ERR_NOT_LITTLE_ENDIAN;
    }

    if (ehdr.e_ehsize != sizeof(Elf64_Ehdr) ||
        (ehdr.e_phnum > 0 && ehdr.e_phentsize != sizeof(Elf64_Phdr)) ||
        (ehdr.e_shoff != 0 && ehdr.e_shentsize != sizeof(Elf64_Shdr))) {
        if (!quiet) printf("ERROR: header entry size mismatch with ABI specification\n");
        fclose(f);
        return ERR_BAD_HEADER_SIZE;
    }

    /*
     * Effective counts. The 16-bit header fields overflow on large objects, so the gABI parks the
     * real values in section header 0: e_shnum == 0 means shdrs[0].sh_size, e_shstrndx == SHN_XINDEX
     * means shdrs[0].sh_link, and e_phnum == PN_XNUM means shdrs[0].sh_info.
     */
    uint64_t shnum = ehdr.e_shnum;
    uint32_t shstrndx = ehdr.e_shstrndx;
    uint32_t phnum = ehdr.e_phnum;
    bool extended = false;
    bool need_sh0 = ehdr.e_shnum == 0 || ehdr.e_shstrndx == SHN_XINDEX || ehdr.e_phnum == PN_XNUM;
    if (need_sh0 && ehdr.e_shoff != 0) {
        Elf64_Shdr sh0;
        if (ehdr.e_shoff > file_size || sizeof(sh0) > file_size - ehdr.e_shoff || !read_shdr(f, ehdr.e_shoff, &sh0)) {
            if (!quiet) printf("ERROR: section header 0 lies beyond file end\n");
            fclose(f);
            return ERR_TRUNCATED_FILE;
        }
        if (ehdr.e_shnum == 0) { shnum = sh0.sh_size; extended = true; }
        if (ehdr.e_shstrndx == SHN_XINDEX) { shstrndx = sh0.sh_link; extended = true; }
        if (ehdr.e_phnum == PN_XNUM) { phnum = sh0.sh_info; extended = true; }
    } else if (ehdr.e_shoff == 0 && (ehdr.e_shstrndx == SHN_XINDEX || ehdr.e_phnum == PN_XNUM)) {
        if (!quiet) printf("ERROR: extended numbering requested but there is no section header 0\n");
        fclose(f);
        return ERR_BAD_EXTENDED;
    }

    if (shnum > 0 && shstrndx >= shnum) {
        if (!quiet) printf("ERROR: e_shstrndx out of bounds (%u >= %lu)\n", shstrndx, (unsigned long)shnum);
        fclose(f);
        return ERR_BAD_SHSTRNDX;
    }

    if (ehdr.e_phnum > 0 && (ehdr.e_phoff > file_size ||
        (uint64_t)phnum * sizeof(Elf64_Phdr) > file_size - ehdr.e_phoff)) {
        if (!quiet) printf("ERROR: program header table extends beyond file end\n");
        fclose(f);
        return ERR_TRUNCATED_FILE;
    }
    if (shnum > 0 && (shnum > file_size / sizeof(Elf64_Shdr) || ehdr.e_shoff > file_size ||
        shnum * sizeof(Elf64_Shdr) > file_size - ehdr.e_shoff)) {
        if (!quiet) printf("ERROR: section header table extends beyond file end\n");
        fclose(f);
        return ERR_TRUNCATED_FILE;
    }

    Elf64_Phdr *phdrs = calloc(phnum ? phnum : 1, sizeof(Elf64_Phdr));
    if (!phdrs) {
        fclose(f);
        return ERR_IO;
    }
    if (phnum > 0) {
        if (fseek(f, (long)ehdr.e_phoff, SEEK_SET) != 0 ||
            fread(phdrs, sizeof(Elf64_Phdr), phnum, f) != phnum) {
            free(phdrs);
            fclose(f);
            return ERR_IO;
        }
    }

    for (uint32_t p = 0; p < phnum; p++) {
        Elf64_Phdr *ph = &phdrs[p];
        if (ph->p_type == PT_LOAD) {
            /* gABI: p_align 0 and 1 both mean "no alignment required". */
            uint64_t al = ph->p_align ? ph->p_align : 1;
            if (al > 1) {
                if ((al & (al - 1)) != 0) {
                    if (!quiet) printf("ERROR: PT_LOAD segment %u p_align is not a power of 2: 0x%lx\n", p, (unsigned long)al);
                    free(phdrs);
                    fclose(f);
                    return ERR_CONGRUENCE_VIOLATION;
                }
                if ((ph->p_vaddr % al) != (ph->p_offset % al)) {
                    if (!quiet) printf("ERROR: PT_LOAD segment %u violates congruence rule: vaddr 0x%lx mod align 0x%lx != offset 0x%lx mod align\n",
                                       p, (unsigned long)ph->p_vaddr, (unsigned long)al, (unsigned long)ph->p_offset);
                    free(phdrs);
                    fclose(f);
                    return ERR_CONGRUENCE_VIOLATION;
                }
            }
        }
    }

    Elf64_Shdr *shdrs = calloc(shnum ? shnum : 1, sizeof(Elf64_Shdr));
    if (!shdrs) {
        free(phdrs);
        fclose(f);
        return ERR_IO;
    }
    if (shnum > 0) {
        if (fseek(f, (long)ehdr.e_shoff, SEEK_SET) != 0 ||
            fread(shdrs, sizeof(Elf64_Shdr), shnum, f) != shnum) {
            free(phdrs);
            free(shdrs);
            fclose(f);
            return ERR_IO;
        }
    }

    /* Section name table: bound every sh_name against it before any name is dereferenced. */
    char *shstrtab = NULL;
    uint64_t shstrtab_size = 0;
    if (shnum > 0 && shstrndx != SHN_UNDEF) {
        Elf64_Shdr *str_sh = &shdrs[shstrndx];
        if (str_sh->sh_type != SHT_STRTAB) {
            if (!quiet) printf("ERROR: e_shstrndx names a section that is not SHT_STRTAB\n");
            free(phdrs);
            free(shdrs);
            fclose(f);
            return ERR_BAD_SHSTRNDX;
        }
        if (str_sh->sh_offset > file_size || str_sh->sh_size > file_size - str_sh->sh_offset) {
            if (!quiet) printf("ERROR: section name table extends beyond file end\n");
            free(phdrs);
            free(shdrs);
            fclose(f);
            return ERR_TRUNCATED_FILE;
        }
        shstrtab_size = str_sh->sh_size;
        shstrtab = calloc(shstrtab_size + 1, 1);
        if (!shstrtab ||
            fseek(f, (long)str_sh->sh_offset, SEEK_SET) != 0 ||
            fread(shstrtab, 1, shstrtab_size, f) != shstrtab_size) {
            free(shstrtab);
            free(phdrs);
            free(shdrs);
            fclose(f);
            return ERR_IO;
        }
        for (uint64_t s = 1; s < shnum; s++) {
            if (shdrs[s].sh_name >= shstrtab_size) {
                if (!quiet) printf("ERROR: section %lu sh_name 0x%x is outside the name table (size 0x%lx)\n",
                                   (unsigned long)s, shdrs[s].sh_name, (unsigned long)shstrtab_size);
                free(shstrtab);
                free(phdrs);
                free(shdrs);
                fclose(f);
                return ERR_BAD_SHNAME;
            }
        }
    }

    if (!quiet) {
        printf("ELF Identification:\n");
        printf("  Magic:   %02x %02x %02x %02x (Valid ELF)\n",
               ehdr.e_ident[EI_MAG0], ehdr.e_ident[EI_MAG1],
               ehdr.e_ident[EI_MAG2], ehdr.e_ident[EI_MAG3]);
        printf("  Class:   ELFCLASS64 (64-bit)\n");
        printf("  Data:    ELFDATA2LSB (Little-Endian)\n");
        printf("  Version: %u (EV_CURRENT)\n", ehdr.e_ident[EI_VERSION]);
        printf("  OS/ABI:  %u (System V / UNIX)\n", ehdr.e_ident[EI_OSABI]);
        printf("  Type:    %s\n", ehdr.e_type == ET_DYN ? "ET_DYN (Position-Independent Shared Object / PIE)" :
                                  ehdr.e_type == ET_EXEC ? "ET_EXEC (Executable)" : "OTHER");
        printf("  Machine: %u\n", ehdr.e_machine);

        printf("\nHeader Invariants:\n");
        printf("  e_ehsize:    %u bytes (ABI struct size %zu) -> MATCH\n", ehdr.e_ehsize, sizeof(Elf64_Ehdr));
        printf("  e_phentsize: %u bytes (ABI struct size %zu) -> MATCH\n", ehdr.e_phentsize, sizeof(Elf64_Phdr));
        printf("  e_shentsize: %u bytes (ABI struct size %zu) -> MATCH\n", ehdr.e_shentsize, sizeof(Elf64_Shdr));
        printf("  e_phnum:     %u program headers\n", phnum);
        printf("  e_shnum:     %lu section headers\n", (unsigned long)shnum);
        printf("  e_shstrndx:  %u (string table index)\n", shstrndx);
        if (extended) printf("  (extended numbering: effective counts read from section header 0)\n");

        printf("\nProgram Headers (Execution View):\n");
        for (uint32_t p = 0; p < phnum; p++) {
            Elf64_Phdr *ph = &phdrs[p];
            char flg[4];
            format_segment_flags(ph->p_flags, flg);
            printf("  [%02u] %-14s flags=%s filesz=0x%lx memsz=0x%lx align=0x%lx",
                   p, get_segment_type_name(ph->p_type), flg,
                   (unsigned long)ph->p_filesz, (unsigned long)ph->p_memsz, (unsigned long)ph->p_align);
            if (ph->p_type == PT_LOAD) {
                uint64_t al = ph->p_align ? ph->p_align : 1;
                uint64_t v_rem = ph->p_vaddr % al;
                uint64_t o_rem = ph->p_offset % al;
                printf(" (congruence: vaddr%%align=%lu == offset%%align=%lu -> OK)", (unsigned long)v_rem, (unsigned long)o_rem);
                if (ph->p_memsz > ph->p_filesz) {
                    printf(" [BSS zero-fill: %lu bytes]", (unsigned long)(ph->p_memsz - ph->p_filesz));
                }
            } else if (ph->p_type == PT_GNU_STACK) {
                printf(" [stack: %s]", (ph->p_flags & PF_X) ? "EXECUTABLE" : "NON-EXECUTABLE (NX)");
            }
            printf("\n");
        }

        printf("\nSection Headers (Linking View):\n");
        for (uint64_t s = 0; s < shnum; s++) {
            Elf64_Shdr *sh = &shdrs[s];
            const char *name = (s == 0) ? "[NULL]" : (shstrtab ? (shstrtab + sh->sh_name) : "UNKNOWN");
            char flg[16];
            format_section_flags(sh->sh_flags, flg);
            printf("  [%02lu] %-20s %-12s flags=%-4s size=0x%lx align=0x%lx\n",
                   (unsigned long)s, name, get_section_type_name(sh->sh_type), flg,
                   (unsigned long)sh->sh_size, (unsigned long)sh->sh_addralign);
        }

        printf("\nSection to Segment Mapping:\n");
        printf("  Segment Sections...\n");
        for (uint32_t p = 0; p < phnum; p++) {
            Elf64_Phdr *ph = &phdrs[p];
            printf("   %02u     ", p);
            for (uint64_t s = 1; s < shnum; s++) {
                Elf64_Shdr *sh = &shdrs[s];
                int in_seg = 0;
                /* The start must lie strictly inside the segment: an empty section at the end is not in it. */
                if (sh->sh_type == SHT_NOBITS) {
                    if ((sh->sh_flags & SHF_ALLOC) &&
                        sh->sh_addr >= ph->p_vaddr &&
                        (sh->sh_addr + sh->sh_size) <= (ph->p_vaddr + ph->p_memsz)) {
                        in_seg = 1;
                    }
                } else {
                    if (sh->sh_offset >= ph->p_offset &&
                        sh->sh_offset < (ph->p_offset + ph->p_filesz) &&
                        (sh->sh_offset + sh->sh_size) <= (ph->p_offset + ph->p_filesz)) {
                        if (sh->sh_flags & SHF_ALLOC) {
                            if (sh->sh_addr >= ph->p_vaddr &&
                                (sh->sh_addr + sh->sh_size) <= (ph->p_vaddr + ph->p_memsz)) {
                                in_seg = 1;
                            }
                        } else {
                            in_seg = 1;
                        }
                    }
                }
                if (in_seg && shstrtab) {
                    printf("%s ", shstrtab + sh->sh_name);
                }
            }
            printf("\n");
        }

        printf("\nAllocated vs Unallocated Section Inventory:\n");
        int alloc_count = 0;
        int non_alloc_count = 0;
        for (uint64_t s = 1; s < shnum; s++) {
            if (shdrs[s].sh_flags & SHF_ALLOC) {
                alloc_count++;
            } else {
                non_alloc_count++;
            }
        }
        printf("  Total Allocatable Sections (loaded into memory): %d\n", alloc_count);
        printf("  Total Non-Allocatable Sections (debug/linking metadata): %d\n", non_alloc_count);
    }

    free(shstrtab);
    free(phdrs);
    free(shdrs);
    fclose(f);
    return ERR_OK;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <elf-file> [--quiet]\n", argv[0]);
        return 1;
    }
    bool quiet = (argc >= 3 && strcmp(argv[2], "--quiet") == 0);
    int rc = parse_elf(argv[1], quiet);
    return rc;
}
