#include "elf_symbolizer.hpp"
#include <iostream>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <cstring>
#include <cxxabi.h>
#include <elf.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>

namespace profiler {

ElfSymbolizer::ElfSymbolizer() {}

bool ElfSymbolizer::init() {
    parse_proc_maps();
    for (auto& mod : modules_) {
        load_module_symbols(mod);
    }
    return true;
}

void ElfSymbolizer::parse_proc_maps() {
    modules_.clear();
    std::ifstream maps("/proc/self/maps");
    if (!maps.is_open()) return;

    std::string line;
    while (std::getline(maps, line)) {
        std::istringstream iss(line);
        std::string addr_range, perms, offset_str, dev, inode_str, path;
        if (!(iss >> addr_range >> perms >> offset_str >> dev >> inode_str)) continue;
        iss >> path; // path may be empty or contain spaces, but standard executable paths don't

        // Only parse executable mappings
        if (perms.find('x') == std::string::npos) continue;
        if (path.empty() || path[0] != '/') continue;

        auto dash = addr_range.find('-');
        if (dash == std::string::npos) continue;

        uintptr_t start = std::stoull(addr_range.substr(0, dash), nullptr, 16);
        uintptr_t end = std::stoull(addr_range.substr(dash + 1), nullptr, 16);
        uintptr_t offset = std::stoull(offset_str, nullptr, 16);

        ModuleMapping mod;
        mod.start_addr = start;
        mod.end_addr = end;
        mod.file_offset = offset;
        mod.path = path;
        mod.load_bias = 0; // Will be refined in load_module_symbols
        modules_.push_back(mod);
    }
}

bool ElfSymbolizer::load_module_symbols(ModuleMapping& mod) {
    int fd = open(mod.path.c_str(), O_RDONLY);
    if (fd < 0) return false;

    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size < static_cast<off_t>(sizeof(Elf64_Ehdr))) {
        close(fd);
        return false;
    }

    void* map_base = mmap(nullptr, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (map_base == MAP_FAILED) return false;

    const char* bytes = static_cast<const char*>(map_base);
    const Elf64_Ehdr* ehdr = reinterpret_cast<const Elf64_Ehdr*>(bytes);

    if (std::memcmp(ehdr->e_ident, ELFMAG, SELFMAG) != 0 || ehdr->e_ident[EI_CLASS] != ELFCLASS64) {
        munmap(map_base, st.st_size);
        return false;
    }

    // Determine load bias from the executable PT_LOAD segment that this mapping covers.
    // The bias is the difference between runtime and link-time addresses, so for a mapping
    // that starts at file offset map_offset:
    //   load_bias = map_start - map_offset + p_offset - p_vaddr
    // Using the r-xp mapping start with min(p_vaddr) is wrong on x86-64, where GNU ld
    // (-z separate-code, binutils 2.31+) puts code in its own segment at a non-zero offset.
    const Elf64_Phdr* phdrs = reinterpret_cast<const Elf64_Phdr*>(bytes + ehdr->e_phoff);
    const uintptr_t map_len = mod.end_addr - mod.start_addr;
    uintptr_t bias = 0;
    for (Elf64_Half i = 0; i < ehdr->e_phnum; ++i) {
        const Elf64_Phdr& ph = phdrs[i];
        if (ph.p_type != PT_LOAD || (ph.p_flags & PF_X) == 0) continue;
        const bool overlaps = ph.p_offset < mod.file_offset + map_len &&
                              ph.p_offset + ph.p_filesz > mod.file_offset;
        if (!overlaps) continue;
        bias = mod.start_addr - mod.file_offset + ph.p_offset - ph.p_vaddr;
        break;
    }
    mod.load_bias = bias;

    // Scan section headers for .symtab or .dynsym
    const Elf64_Shdr* shdrs = reinterpret_cast<const Elf64_Shdr*>(bytes + ehdr->e_shoff);
    const Elf64_Shdr* symtab_shdr = nullptr;
    const Elf64_Shdr* dynsym_shdr = nullptr;

    for (Elf64_Half i = 0; i < ehdr->e_shnum; ++i) {
        if (shdrs[i].sh_type == SHT_SYMTAB) {
            symtab_shdr = &shdrs[i];
        } else if (shdrs[i].sh_type == SHT_DYNSYM) {
            dynsym_shdr = &shdrs[i];
        }
    }

    // Prefer .symtab if present; otherwise fallback to .dynsym
    const Elf64_Shdr* chosen_symtab = symtab_shdr ? symtab_shdr : dynsym_shdr;
    if (chosen_symtab && chosen_symtab->sh_link < ehdr->e_shnum) {
        const Elf64_Shdr* strtab_shdr = &shdrs[chosen_symtab->sh_link];
        const char* strtab = bytes + strtab_shdr->sh_offset;
        const Elf64_Sym* syms = reinterpret_cast<const Elf64_Sym*>(bytes + chosen_symtab->sh_offset);
        size_t count = chosen_symtab->sh_size / sizeof(Elf64_Sym);

        for (size_t i = 0; i < count; ++i) {
            const Elf64_Sym& sym = syms[i];
            unsigned char st_type = ELF64_ST_TYPE(sym.st_info);
            if (st_type == STT_FUNC || (st_type == STT_NOTYPE && sym.st_value != 0)) {
                if (sym.st_name < strtab_shdr->sh_size) {
                    const char* raw_name = strtab + sym.st_name;
                    // Ignore empty names and architecture mapping symbols ($x, $d, $t, etc.)
                    if (raw_name[0] != '\0' && raw_name[0] != '$') {
                        // Demangle C++ names if possible
                        int status = 0;
                        char* demangled = abi::__cxa_demangle(raw_name, nullptr, nullptr, &status);
                        std::string sym_name = (status == 0 && demangled) ? demangled : raw_name;
                        if (demangled) free(demangled);

                        mod.symbols.push_back({sym.st_value, sym.st_size, sym_name});
                    }
                }
            }
        }
    }

    // Sort symbols by vaddr for binary search
    std::sort(mod.symbols.begin(), mod.symbols.end(), [](const Symbol& a, const Symbol& b) {
        return a.vaddr < b.vaddr;
    });

    munmap(map_base, st.st_size);
    return true;
}

std::string ElfSymbolizer::resolve(uintptr_t ip) {
    // 1. Locate module containing IP
    const ModuleMapping* target_mod = nullptr;
    for (const auto& mod : modules_) {
        if (ip >= mod.start_addr && ip < mod.end_addr) {
            target_mod = &mod;
            break;
        }
    }

    if (!target_mod) {
        return "[unknown]";
    }

    uintptr_t rel_addr = ip - target_mod->load_bias;

    // 2. Binary search symbols
    if (!target_mod->symbols.empty()) {
        auto it = std::upper_bound(target_mod->symbols.begin(), target_mod->symbols.end(), rel_addr,
            [](uintptr_t val, const Symbol& sym) {
                return val < sym.vaddr;
            });

        if (it != target_mod->symbols.begin()) {
            --it;
            // Check if rel_addr is within symbol bounds (if size is known)
            if (it->size == 0 || (rel_addr >= it->vaddr && rel_addr < it->vaddr + it->size)) {
                return it->name;
            }
            // Even if slightly past size, if close enough (within 4KB), return with offset
            if (rel_addr >= it->vaddr && rel_addr < it->vaddr + 4096) {
                return it->name;
            }
        }
    }

    // Fallback: basename of module + hex offset
    auto slash = target_mod->path.rfind('/');
    std::string bname = (slash != std::string::npos) ? target_mod->path.substr(slash + 1) : target_mod->path;
    std::ostringstream oss;
    oss << bname << "+0x" << std::hex << (ip - target_mod->start_addr);
    return oss.str();
}

} // namespace profiler
