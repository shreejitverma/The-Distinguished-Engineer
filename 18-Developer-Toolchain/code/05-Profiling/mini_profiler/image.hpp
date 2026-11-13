// Minimal readers for the two object formats a profiler on Linux and macOS must symbolize:
// ELF64 (.symtab or .dynsym, PT_LOAD, the GNU build ID note) and Mach-O 64 (LC_SYMTAB,
// LC_SEGMENT_64, LC_UUID, LC_FUNCTION_STARTS, universal files).
//
// The structures are declared here instead of taken from <elf.h> or <mach-o/loader.h>, so the
// symbolizer builds on either system and can symbolize a profile recorded on the other one.
// Layouts follow the System V gABI (ELF) and Apple's <mach-o/loader.h>, <mach-o/nlist.h>,
// and <mach-o/fat.h>. Little-endian images only; every read is bounds-checked.
#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

namespace image {

struct Symbol {
    std::uint64_t addr = 0; // link-time virtual address
    std::uint64_t size = 0; // ELF st_size; always 0 for Mach-O, which stores no sizes
    std::string name;       // as stored: mangled, with the Mach-O leading underscore
};

struct Segment {
    std::uint64_t vmaddr = 0;
    std::uint64_t fileoff = 0;
    std::uint64_t filesize = 0;
};

enum class Format { elf64, macho64 };

struct Image {
    Format format = Format::elf64;
    std::string symbol_source;                  // ".symtab", ".dynsym", "LC_SYMTAB", or "none"
    std::string identity = "-";                 // GNU build ID or LC_UUID, lowercase hex
    std::vector<Segment> segments;              // ELF PT_LOAD or Mach-O LC_SEGMENT_64
    std::uint64_t text_vmaddr = 0;              // Mach-O: vmaddr of __TEXT
    std::vector<Symbol> symbols;                // defined function symbols, sorted by addr
    std::vector<std::uint64_t> function_starts; // Mach-O LC_FUNCTION_STARTS, sorted
};

namespace detail {

using Bytes = std::vector<unsigned char>;

// ELF64, System V gABI.
struct Elf64_Ehdr {
    unsigned char e_ident[16];
    std::uint16_t e_type, e_machine;
    std::uint32_t e_version;
    std::uint64_t e_entry, e_phoff, e_shoff;
    std::uint32_t e_flags;
    std::uint16_t e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx;
};
struct Elf64_Phdr {
    std::uint32_t p_type, p_flags;
    std::uint64_t p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align;
};
struct Elf64_Shdr {
    std::uint32_t sh_name, sh_type;
    std::uint64_t sh_flags, sh_addr, sh_offset, sh_size;
    std::uint32_t sh_link, sh_info;
    std::uint64_t sh_addralign, sh_entsize;
};
struct Elf64_Sym {
    std::uint32_t st_name;
    unsigned char st_info, st_other;
    std::uint16_t st_shndx;
    std::uint64_t st_value, st_size;
};
static_assert(sizeof(Elf64_Ehdr) == 64 && sizeof(Elf64_Phdr) == 56, "ELF64 header layout");
static_assert(sizeof(Elf64_Shdr) == 64 && sizeof(Elf64_Sym) == 24, "ELF64 section and symbol layout");

constexpr std::uint32_t kPtLoad = 1, kPtNote = 4;
constexpr std::uint32_t kShtSymtab = 2, kShtDynsym = 11;
constexpr unsigned kSttFunc = 2, kSttGnuIfunc = 10;
constexpr std::uint32_t kNtGnuBuildId = 3;

// Mach-O 64, <mach-o/loader.h>, <mach-o/nlist.h>, <mach-o/fat.h>.
struct mach_header_64 {
    std::uint32_t magic;
    std::int32_t cputype, cpusubtype;
    std::uint32_t filetype, ncmds, sizeofcmds, flags, reserved;
};
struct load_command {
    std::uint32_t cmd, cmdsize;
};
struct segment_command_64 {
    std::uint32_t cmd, cmdsize;
    char segname[16];
    std::uint64_t vmaddr, vmsize, fileoff, filesize;
    std::int32_t maxprot, initprot;
    std::uint32_t nsects, flags;
};
struct symtab_command {
    std::uint32_t cmd, cmdsize, symoff, nsyms, stroff, strsize;
};
struct linkedit_data_command {
    std::uint32_t cmd, cmdsize, dataoff, datasize;
};
struct nlist_64 {
    std::uint32_t n_strx;
    std::uint8_t n_type, n_sect;
    std::uint16_t n_desc;
    std::uint64_t n_value;
};
static_assert(sizeof(mach_header_64) == 32 && sizeof(segment_command_64) == 72, "Mach-O header layout");
static_assert(sizeof(nlist_64) == 16, "Mach-O symbol layout");

constexpr std::uint32_t kMhMagic64 = 0xfeedfacf;
constexpr std::uint32_t kFatMagic = 0xcafebabe, kFatMagic64 = 0xcafebabf; // stored big-endian
constexpr std::uint32_t kLcSymtab = 0x2, kLcSegment64 = 0x19, kLcUuid = 0x1b, kLcFunctionStarts = 0x26;
constexpr std::uint8_t kNStab = 0xe0, kNType = 0x0e, kNSect = 0x0e;

template <typename T> bool read_at(const Bytes& b, std::uint64_t off, T* out) {
    if (off > b.size() || b.size() - off < sizeof(T)) {
        return false;
    }
    std::memcpy(out, b.data() + off, sizeof(T));
    return true;
}

inline std::uint32_t be32(const Bytes& b, std::uint64_t off) {
    return (std::uint32_t{b[off]} << 24) | (std::uint32_t{b[off + 1]} << 16) | (std::uint32_t{b[off + 2]} << 8) |
           std::uint32_t{b[off + 3]};
}

inline std::uint64_t be64(const Bytes& b, std::uint64_t off) {
    return (std::uint64_t{be32(b, off)} << 32) | be32(b, off + 4);
}

inline std::string hex_of(const unsigned char* p, std::size_t n) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (std::size_t i = 0; i < n; ++i) {
        out.push_back(digits[p[i] >> 4]);
        out.push_back(digits[p[i] & 15]);
    }
    return out.empty() ? "-" : out;
}

// A NUL-terminated string that starts at off and ends before limit. Names are capped, so a
// string table without terminators cannot make every symbol cost megabytes.
inline std::string cstring_at(const Bytes& b, std::uint64_t off, std::uint64_t limit) {
    constexpr std::size_t kMaxName = 4096;
    limit = std::min<std::uint64_t>(limit, b.size());
    std::string out;
    while (off < limit && b[off] != 0 && out.size() < kMaxName) {
        out.push_back(static_cast<char>(b[off++]));
    }
    return out;
}

inline void sort_symbols(Image* img) {
    std::sort(img->symbols.begin(), img->symbols.end(), [](const Symbol& a, const Symbol& b) {
        return a.addr != b.addr ? a.addr < b.addr : a.name < b.name;
    });
}

inline bool parse_elf(const Bytes& b, Image* img, std::string* error) {
    Elf64_Ehdr eh;
    if (!read_at(b, 0, &eh) || eh.e_ident[4] != 2 /* ELFCLASS64 */ || eh.e_ident[5] != 1 /* ELFDATA2LSB */) {
        *error = "not a little-endian ELF64 file";
        return false;
    }
    img->format = Format::elf64;
    for (unsigned i = 0; i < eh.e_phnum; ++i) {
        Elf64_Phdr ph;
        if (!read_at(b, eh.e_phoff + std::uint64_t{i} * eh.e_phentsize, &ph)) {
            *error = "program header table runs past the end of the file";
            return false;
        }
        if (ph.p_type == kPtLoad) {
            img->segments.push_back({ph.p_vaddr, ph.p_offset, ph.p_filesz});
        } else if (ph.p_type == kPtNote) {
            // Note: namesz, descsz, type, then the name and the descriptor, each padded to 4 bytes.
            std::uint64_t off = ph.p_offset;
            const std::uint64_t end = std::min<std::uint64_t>(ph.p_offset + ph.p_filesz, b.size());
            while (off + 12 <= end) {
                std::uint32_t namesz = 0, descsz = 0, type = 0;
                read_at(b, off, &namesz);
                read_at(b, off + 4, &descsz);
                read_at(b, off + 8, &type);
                const std::uint64_t name = off + 12;
                const std::uint64_t desc = name + ((std::uint64_t{namesz} + 3) & ~std::uint64_t{3});
                if (desc + descsz > end) {
                    break;
                }
                if (type == kNtGnuBuildId && namesz == 4 && std::memcmp(b.data() + name, "GNU", 4) == 0) {
                    img->identity = hex_of(b.data() + desc, descsz);
                }
                off = desc + ((std::uint64_t{descsz} + 3) & ~std::uint64_t{3});
            }
        }
    }
    // Prefer .symtab (every function, including static ones); fall back to .dynsym (exports only).
    Elf64_Shdr table{};
    bool found = false;
    for (std::uint32_t wanted : {kShtSymtab, kShtDynsym}) {
        for (unsigned i = 0; i < eh.e_shnum && !found; ++i) {
            Elf64_Shdr sh;
            if (!read_at(b, eh.e_shoff + std::uint64_t{i} * eh.e_shentsize, &sh)) {
                break;
            }
            if (sh.sh_type == wanted && sh.sh_entsize == sizeof(Elf64_Sym)) {
                table = sh;
                found = true;
                img->symbol_source = wanted == kShtSymtab ? ".symtab" : ".dynsym";
            }
        }
        if (found) {
            break;
        }
    }
    if (!found) {
        img->symbol_source = "none";
        return true;
    }
    Elf64_Shdr strtab; // sh_link of a symbol table names its string table
    if (!read_at(b, eh.e_shoff + std::uint64_t{table.sh_link} * eh.e_shentsize, &strtab)) {
        *error = "symbol table links to a string table outside the section header table";
        return false;
    }
    const std::uint64_t count = table.sh_size / sizeof(Elf64_Sym);
    for (std::uint64_t i = 0; i < count; ++i) {
        Elf64_Sym sym;
        if (!read_at(b, table.sh_offset + i * sizeof(Elf64_Sym), &sym)) {
            *error = "symbol table runs past the end of the file";
            return false;
        }
        const unsigned type = sym.st_info & 0xf;
        if ((type != kSttFunc && type != kSttGnuIfunc) || sym.st_shndx == 0 /* SHN_UNDEF */) {
            continue;
        }
        img->symbols.push_back(
            {sym.st_value, sym.st_size, cstring_at(b, strtab.sh_offset + sym.st_name, strtab.sh_offset + strtab.sh_size)});
    }
    sort_symbols(img);
    return true;
}

// Parses one thin Mach-O image that starts at base and is size bytes long.
inline bool parse_macho(const Bytes& b, std::uint64_t base, std::uint64_t size, Image* img, std::string* error) {
    mach_header_64 mh;
    if (!read_at(b, base, &mh) || mh.magic != kMhMagic64) {
        *error = "not a little-endian 64-bit Mach-O image";
        return false;
    }
    img->format = Format::macho64;
    img->symbol_source = "none";
    symtab_command symtab{};
    linkedit_data_command starts{};
    bool have_text = false;
    std::uint64_t off = base + sizeof(mach_header_64);
    for (std::uint32_t i = 0; i < mh.ncmds; ++i) {
        load_command lc;
        if (!read_at(b, off, &lc) || lc.cmdsize < sizeof(load_command)) {
            *error = "load command runs past the end of the file";
            return false;
        }
        if (lc.cmd == kLcSegment64) {
            segment_command_64 seg;
            if (read_at(b, off, &seg)) {
                img->segments.push_back({seg.vmaddr, seg.fileoff, seg.filesize});
                if (std::strncmp(seg.segname, "__TEXT", sizeof(seg.segname)) == 0) {
                    img->text_vmaddr = seg.vmaddr;
                    have_text = true;
                }
            }
        } else if (lc.cmd == kLcSymtab) {
            read_at(b, off, &symtab);
        } else if (lc.cmd == kLcFunctionStarts) {
            read_at(b, off, &starts);
        } else if (lc.cmd == kLcUuid && lc.cmdsize >= sizeof(load_command) + 16 &&
                   off + sizeof(load_command) + 16 <= b.size()) {
            img->identity = hex_of(b.data() + off + sizeof(load_command), 16);
        }
        off += lc.cmdsize;
    }
    if (!have_text) {
        *error = "Mach-O image has no __TEXT segment";
        return false;
    }
    // LC_FUNCTION_STARTS: ULEB128 deltas. The first is the offset of the first function from
    // the start of __TEXT, each later one the distance from the previous function; 0 ends the list.
    // load() guarantees base + size <= b.size(), so this bound keeps every read inside the file.
    std::uint64_t p = base + starts.dataoff;
    const std::uint64_t starts_end = std::min<std::uint64_t>(p + starts.datasize, base + size);
    std::uint64_t address = img->text_vmaddr;
    while (starts.datasize != 0 && p < starts_end) {
        std::uint64_t delta = 0;
        unsigned shift = 0;
        while (p < starts_end) {
            const unsigned char byte = b[p++];
            delta |= std::uint64_t{byte & 0x7fu} << shift;
            shift += 7;
            if ((byte & 0x80u) == 0 || shift >= 64) {
                break;
            }
        }
        if (delta == 0) {
            break;
        }
        address += delta;
        img->function_starts.push_back(address);
    }
    // LC_SYMTAB: nsyms nlist_64 entries at symoff, names in the string table at stroff.
    for (std::uint32_t i = 0; i < symtab.nsyms; ++i) {
        nlist_64 nl;
        if (!read_at(b, base + symtab.symoff + std::uint64_t{i} * sizeof(nlist_64), &nl)) {
            *error = "symbol table runs past the end of the file";
            return false;
        }
        if ((nl.n_type & kNStab) != 0 || (nl.n_type & kNType) != kNSect) {
            continue; // debugger (stab) entries and undefined, absolute, or indirect symbols
        }
        // Keep code symbols only. When the image lists its function starts, a symbol is code if
        // it sits on one; otherwise any symbol defined in a section is kept.
        if (!img->function_starts.empty() &&
            !std::binary_search(img->function_starts.begin(), img->function_starts.end(), nl.n_value)) {
            continue;
        }
        const std::uint64_t str = base + symtab.stroff;
        img->symbols.push_back({nl.n_value, 0, cstring_at(b, str + nl.n_strx, str + symtab.strsize)});
    }
    if (symtab.nsyms != 0) {
        img->symbol_source = "LC_SYMTAB";
    }
    sort_symbols(img);
    return true;
}

} // namespace detail

// Loads the image at path. A universal (fat) Mach-O file holds one image per architecture:
// the slice whose LC_UUID equals want_identity is chosen, or the first slice when none matches.
inline std::optional<Image> load(const std::string& path, const std::string& want_identity, std::string* error) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (f == nullptr) {
        *error = "cannot open " + path;
        return std::nullopt;
    }
    detail::Bytes b;
    unsigned char chunk[65536];
    std::size_t got = 0;
    while ((got = std::fread(chunk, 1, sizeof(chunk), f)) > 0) {
        b.insert(b.end(), chunk, chunk + got);
    }
    const bool read_failed = std::ferror(f) != 0;
    std::fclose(f);
    if (read_failed) {
        *error = "read error on " + path;
        return std::nullopt;
    }
    if (b.size() < 8) {
        *error = path + " is too short to be an object file";
        return std::nullopt;
    }
    Image img;
    if (std::memcmp(b.data(), "\x7f"
                              "ELF",
                    4) == 0) {
        if (!detail::parse_elf(b, &img, error)) {
            return std::nullopt;
        }
        return img;
    }
    const std::uint32_t magic_be = detail::be32(b, 0);
    if (magic_be == detail::kFatMagic || magic_be == detail::kFatMagic64) {
        const bool wide = magic_be == detail::kFatMagic64;
        const std::uint32_t count = detail::be32(b, 4);
        const std::uint64_t entry = wide ? 32 : 20; // sizeof(fat_arch_64) : sizeof(fat_arch)
        std::optional<Image> first;
        for (std::uint32_t i = 0; i < count; ++i) {
            const std::uint64_t at = 8 + i * entry;
            if (at + entry > b.size()) {
                break;
            }
            const std::uint64_t offset = wide ? detail::be64(b, at + 8) : detail::be32(b, at + 8);
            const std::uint64_t size = wide ? detail::be64(b, at + 16) : detail::be32(b, at + 12);
            if (offset > b.size() || size > b.size() - offset) {
                continue; // the header describes a slice that the file does not contain
            }
            Image slice;
            std::string slice_error;
            if (!detail::parse_macho(b, offset, size, &slice, &slice_error)) {
                continue;
            }
            if (slice.identity == want_identity) {
                return slice;
            }
            if (!first) {
                first = slice;
            }
        }
        if (!first) {
            *error = path + " is a universal file with no readable 64-bit slice";
        }
        return first;
    }
    if (!detail::parse_macho(b, 0, b.size(), &img, error)) {
        return std::nullopt;
    }
    return img;
}

} // namespace image
