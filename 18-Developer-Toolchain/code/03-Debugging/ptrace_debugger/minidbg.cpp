// minidbg.cpp - a minimal Linux ptrace debugger with a DWARF CFI (.eh_frame) unwinder.
//
// Modes (run.sh drives all three):
//   minidbg launch TARGET          fork + PTRACE_TRACEME + execv, breakpoint on leaf_compute,
//                                  register read and write, single-step over the breakpoint,
//                                  CFI backtrace versus frame-pointer walk, exit status.
//   minidbg unwind TARGET OFFSET   same launch, breakpoint at link-time OFFSET, CFI backtrace only;
//                                  used on a stripped copy that has no .symtab.
//   minidbg attach TARGET          start TARGET --spin untraced, then PTRACE_SEIZE and
//                                  PTRACE_INTERRUPT it, read memory, PTRACE_DETACH.
//
// Supported hosts: Linux on x86-64 (int3, decrement pc by 1) and AArch64 (brk #0, pc not advanced).
// Everything printed on stdout is deterministic: names, small integers, and rule strings, never
// absolute addresses or PIDs, so the output can be diffed against expected/<platform>.txt.

#include <elf.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/ptrace.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <map>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

namespace {

// ---------------------------------------------------------------------------------------------
// Errors: any failed system call or malformed input ends the run with exit 1, so the harness
// reports FAIL instead of diffing partial output.

[[noreturn]] void fail(const std::string& what) {
    std::fflush(stdout);
    std::fprintf(stderr, "minidbg: %s\n", what.c_str());
    std::exit(1);
}

[[noreturn]] void fail_errno(const std::string& what) { fail(what + ": " + std::strerror(errno)); }

void require(bool ok, const std::string& what) {
    if (!ok) fail(what);
}

// ---------------------------------------------------------------------------------------------
// Architecture description. Register numbers are DWARF numbers: x86-64 psABI "DWARF Register
// Number Mapping" (0 rax ... 7 rsp, 8-15 r8-r15, 16 return address) and AArch64 aadwarf64
// (0-30 x0-x30, 31 sp).

#if defined(__x86_64__)
constexpr const char* kArchName = "x86-64";
constexpr uint16_t kElfMachine = EM_X86_64;
constexpr int kNumRegs = 17;      // DWARF 0-16
constexpr int kSpReg = 7;         // rsp
constexpr int kFpReg = 6;         // rbp
constexpr int kTrapLen = 1;       // int3 is one byte: 0xcc
constexpr uint64_t kTrapBytes = 0xcc;
constexpr int kPcAfterTrap = 1;   // int3 is a trap: rip points after it
constexpr const char* kTrapName = "int3 (encoding cc)";
constexpr const char* kArg0Name = "rdi";
constexpr const char* kArg1Name = "rsi";
const char* const kRegNames[kNumRegs] = {"rax", "rdx", "rcx", "rbx", "rsi", "rdi", "rbp", "rsp", "r8",
                                         "r9",  "r10", "r11", "r12", "r13", "r14", "r15", "rip"};
using RawRegs = user_regs_struct;
uint64_t raw_pc(const RawRegs& r) { return r.rip; }
void set_raw_pc(RawRegs& r, uint64_t pc) { r.rip = pc; }
uint64_t raw_arg(const RawRegs& r, int i) { return i == 0 ? r.rdi : r.rsi; }
void set_raw_arg(RawRegs& r, int i, uint64_t v) { (i == 0 ? r.rdi : r.rsi) = v; }
std::array<uint64_t, kNumRegs> to_dwarf(const RawRegs& r) {
    return {r.rax, r.rdx, r.rcx, r.rbx, r.rsi, r.rdi, r.rbp, r.rsp, r.r8,
            r.r9,  r.r10, r.r11, r.r12, r.r13, r.r14, r.r15, r.rip};
}
#elif defined(__aarch64__)
constexpr const char* kArchName = "aarch64";
constexpr uint16_t kElfMachine = EM_AARCH64;
constexpr int kNumRegs = 32;      // DWARF 0-31
constexpr int kSpReg = 31;        // sp
constexpr int kFpReg = 29;        // x29
constexpr int kTrapLen = 4;       // brk #0 is one 32-bit instruction
constexpr uint64_t kTrapBytes = 0xd4200000;
constexpr int kPcAfterTrap = 0;   // brk is an exception: pc still points at it
constexpr const char* kTrapName = "brk #0 (encoding d4200000)";
constexpr const char* kArg0Name = "x0";
constexpr const char* kArg1Name = "x1";
const char* const kRegNames[kNumRegs] = {
    "x0",  "x1",  "x2",  "x3",  "x4",  "x5",  "x6",  "x7",  "x8",  "x9",  "x10",
    "x11", "x12", "x13", "x14", "x15", "x16", "x17", "x18", "x19", "x20", "x21",
    "x22", "x23", "x24", "x25", "x26", "x27", "x28", "x29", "x30", "sp"};
using RawRegs = user_regs_struct;  // glibc: regs[31], sp, pc, pstate (same layout as user_pt_regs)
uint64_t raw_pc(const RawRegs& r) { return r.pc; }
void set_raw_pc(RawRegs& r, uint64_t pc) { r.pc = pc; }
uint64_t raw_arg(const RawRegs& r, int i) { return r.regs[i]; }
void set_raw_arg(RawRegs& r, int i, uint64_t v) { r.regs[i] = v; }
std::array<uint64_t, kNumRegs> to_dwarf(const RawRegs& r) {
    std::array<uint64_t, kNumRegs> out{};
    for (int i = 0; i < 31; ++i) out[i] = r.regs[i];
    out[31] = r.sp;
    return out;
}
#ifndef NT_ARM_PAC_MASK
#define NT_ARM_PAC_MASK 0x406
#endif
#else
#error "minidbg supports Linux x86-64 and AArch64 only"
#endif

std::string reg_name(uint64_t dwarf_reg) {
    if (dwarf_reg < static_cast<uint64_t>(kNumRegs)) return kRegNames[dwarf_reg];
    return "r" + std::to_string(dwarf_reg);
}

// ---------------------------------------------------------------------------------------------
// Bounds-checked little-endian reader with LEB128, used for ELF tables and .eh_frame.

class Reader {
   public:
    Reader(const uint8_t* begin, const uint8_t* end) : begin_(begin), cur_(begin), end_(end) {}
    bool done() const { return cur_ >= end_; }
    size_t offset() const { return static_cast<size_t>(cur_ - begin_); }
    size_t remaining() const { return static_cast<size_t>(end_ - cur_); }
    const uint8_t* cur() const { return cur_; }
    void seek(const uint8_t* p) {
        require(p >= begin_ && p <= end_, "reader seek out of range");
        cur_ = p;
    }
    void skip(size_t n) {
        need(n);
        cur_ += n;
    }
    uint64_t uint(size_t n) {
        need(n);
        uint64_t v = 0;
        for (size_t i = 0; i < n; ++i) v |= static_cast<uint64_t>(cur_[i]) << (8 * i);
        cur_ += n;
        return v;
    }
    int64_t sint(size_t n) {
        uint64_t v = uint(n);
        const unsigned shift = static_cast<unsigned>(64 - 8 * n);
        return shift == 0 ? static_cast<int64_t>(v) : static_cast<int64_t>(v << shift) >> shift;
    }
    uint8_t u8() { return static_cast<uint8_t>(uint(1)); }
    uint64_t uleb() {
        uint64_t v = 0;
        unsigned shift = 0;
        for (;;) {
            const uint8_t b = u8();
            if (shift < 64) v |= static_cast<uint64_t>(b & 0x7f) << shift;
            shift += 7;
            if ((b & 0x80) == 0) return v;
        }
    }
    int64_t sleb() {
        int64_t v = 0;
        unsigned shift = 0;
        uint8_t b = 0;
        do {
            b = u8();
            if (shift < 64) v |= static_cast<int64_t>(static_cast<uint64_t>(b & 0x7f) << shift);
            shift += 7;
        } while (b & 0x80);
        if (shift < 64 && (b & 0x40)) v |= static_cast<int64_t>(~uint64_t{0} << shift);  // sign-extend
        return v;
    }
    std::string cstr() {
        std::string s;
        for (char c = static_cast<char>(u8()); c != '\0'; c = static_cast<char>(u8())) s.push_back(c);
        return s;
    }

   private:
    void need(size_t n) const { require(static_cast<size_t>(end_ - cur_) >= n, "truncated input"); }
    const uint8_t* begin_;
    const uint8_t* cur_;
    const uint8_t* end_;
};

// ---------------------------------------------------------------------------------------------
// ELF: symbols from .symtab, unwind tables from .eh_frame. Section headers are enough for a
// debugger that has the file; an in-process unwinder would find .eh_frame_hdr through the
// PT_GNU_EH_FRAME program header instead, because section headers are not loaded.

struct Symbol {
    std::string name;
    uint64_t value = 0;  // link-time virtual address
    uint64_t size = 0;
    unsigned type = 0;   // STT_FUNC, STT_OBJECT, ...
};

struct ElfImage {
    std::vector<uint8_t> bytes;
    uint16_t type = 0;
    uint64_t entry = 0;
    std::vector<Symbol> symbols;
    bool has_symtab = false;
    bool has_debug_info = false;
    const uint8_t* eh_frame = nullptr;
    size_t eh_frame_size = 0;
    uint64_t eh_frame_vaddr = 0;

    std::optional<Symbol> find(const std::string& name) const {
        for (const Symbol& s : symbols)
            if (s.name == name) return s;
        return std::nullopt;
    }
    const Symbol* function_at(uint64_t link_addr) const {
        for (const Symbol& s : symbols)
            if (s.type == STT_FUNC && link_addr >= s.value && link_addr < s.value + s.size) return &s;
        return nullptr;
    }
};

ElfImage load_elf(const std::string& path) {
    ElfImage img;
    std::ifstream in(path, std::ios::binary);
    if (!in) fail_errno("open " + path);
    img.bytes.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    const std::vector<uint8_t>& b = img.bytes;
    require(b.size() >= sizeof(Elf64_Ehdr), path + ": too small for an ELF header");
    Elf64_Ehdr eh;
    std::memcpy(&eh, b.data(), sizeof eh);
    require(std::memcmp(eh.e_ident, ELFMAG, SELFMAG) == 0, path + ": not ELF");
    require(eh.e_ident[EI_CLASS] == ELFCLASS64 && eh.e_ident[EI_DATA] == ELFDATA2LSB,
            path + ": not little-endian ELF64");
    require(eh.e_machine == kElfMachine, path + ": built for another machine");
    img.type = eh.e_type;
    img.entry = eh.e_entry;
    require(eh.e_shentsize == sizeof(Elf64_Shdr), path + ": unexpected section header size");
    // Every range check is written as `off <= size && len <= size - off` so it cannot wrap.
    auto fits = [&](uint64_t off, uint64_t len) { return off <= b.size() && len <= b.size() - off; };
    require(fits(eh.e_shoff, static_cast<uint64_t>(eh.e_shnum) * sizeof(Elf64_Shdr)),
            path + ": section headers out of range");
    std::vector<Elf64_Shdr> sh(eh.e_shnum);
    std::memcpy(sh.data(), b.data() + eh.e_shoff, sh.size() * sizeof(Elf64_Shdr));
    require(eh.e_shstrndx < sh.size(), path + ": bad e_shstrndx");
    // A NOBITS section (.bss) has no file bytes, so it is in range only as long as nobody reads it.
    auto in_file = [&](const Elf64_Shdr& s) { return s.sh_type == SHT_NOBITS || fits(s.sh_offset, s.sh_size); };
    auto has_bytes = [&](const Elf64_Shdr& s) { return s.sh_type != SHT_NOBITS && fits(s.sh_offset, s.sh_size); };
    const Elf64_Shdr& shstr = sh[eh.e_shstrndx];
    require(has_bytes(shstr), path + ": .shstrtab out of range");
    auto sec_name = [&](const Elf64_Shdr& s) {
        require(s.sh_name < shstr.sh_size, path + ": bad section name offset");
        const char* p = reinterpret_cast<const char*>(b.data() + shstr.sh_offset + s.sh_name);
        return std::string(p, strnlen(p, shstr.sh_size - s.sh_name));
    };
    for (const Elf64_Shdr& s : sh) {
        require(in_file(s), path + ": section out of range");
        const std::string name = sec_name(s);
        if (name == ".eh_frame") {
            require(has_bytes(s), path + ": .eh_frame has no file bytes");
            img.eh_frame = b.data() + s.sh_offset;
            img.eh_frame_size = s.sh_size;
            img.eh_frame_vaddr = s.sh_addr;
        }
        if (name == ".debug_info") img.has_debug_info = true;
        if (s.sh_type != SHT_SYMTAB) continue;
        img.has_symtab = true;
        require(s.sh_link < sh.size() && s.sh_entsize == sizeof(Elf64_Sym), path + ": bad .symtab");
        const Elf64_Shdr& str = sh[s.sh_link];
        require(has_bytes(s) && has_bytes(str), path + ": .symtab or its string table out of range");
        for (uint64_t off = 0; off + sizeof(Elf64_Sym) <= s.sh_size; off += sizeof(Elf64_Sym)) {
            Elf64_Sym sym;
            std::memcpy(&sym, b.data() + s.sh_offset + off, sizeof sym);
            const unsigned t = ELF64_ST_TYPE(sym.st_info);
            if ((t != STT_FUNC && t != STT_OBJECT) || sym.st_shndx == SHN_UNDEF) continue;
            require(sym.st_name < str.sh_size, path + ": bad symbol name offset");
            const char* p = reinterpret_cast<const char*>(b.data() + str.sh_offset + sym.st_name);
            img.symbols.push_back({std::string(p, strnlen(p, str.sh_size - sym.st_name)), sym.st_value,
                                   sym.st_size, t});
        }
    }
    require(img.eh_frame != nullptr, path + ": no .eh_frame section");
    return img;
}

// ---------------------------------------------------------------------------------------------
// .eh_frame parsing: DWARF 5 section 6.4.1 CIE and FDE layout with the GNU .eh_frame changes
// (CIE id 0, CIE pointer relative to its own field, 'z' augmentation, DW_EH_PE_* encodings).

constexpr uint8_t DW_EH_PE_omit = 0xff;
constexpr uint8_t DW_EH_PE_absptr = 0x00, DW_EH_PE_uleb128 = 0x01, DW_EH_PE_udata2 = 0x02,
                  DW_EH_PE_udata4 = 0x03, DW_EH_PE_udata8 = 0x04, DW_EH_PE_sleb128 = 0x09,
                  DW_EH_PE_sdata2 = 0x0a, DW_EH_PE_sdata4 = 0x0b, DW_EH_PE_sdata8 = 0x0c;
constexpr uint8_t DW_EH_PE_pcrel = 0x10, DW_EH_PE_indirect = 0x80;

constexpr int kMaxCfiReg = 128;  // covers x86-64 xmm and AArch64 v registers that CFI may mention

struct Cie {
    uint8_t version = 0;
    std::string augmentation;
    uint64_t code_align = 0;
    int64_t data_align = 0;
    uint64_t ra_reg = 0;
    uint8_t fde_encoding = DW_EH_PE_absptr;
    bool has_z = false;
    const uint8_t* insns = nullptr;
    const uint8_t* insns_end = nullptr;
};

struct Fde {
    size_t cie_offset = 0;
    uint64_t pc_begin = 0;  // link-time address
    uint64_t pc_range = 0;
    const uint8_t* insns = nullptr;
    const uint8_t* insns_end = nullptr;
};

// Read a pointer in the given DW_EH_PE encoding. `apply` is false when the value is only skipped
// (a personality pointer), so an indirect or unsupported application does not matter.
uint64_t read_encoded(Reader& r, uint8_t enc, uint64_t section_vaddr, bool apply = true) {
    require(enc != DW_EH_PE_omit, "omitted pointer where one is required");
    const uint64_t field_vaddr = section_vaddr + r.offset();
    uint64_t v = 0;
    switch (enc & 0x0f) {
        case DW_EH_PE_absptr: v = r.uint(8); break;
        case DW_EH_PE_uleb128: v = r.uleb(); break;
        case DW_EH_PE_udata2: v = r.uint(2); break;
        case DW_EH_PE_udata4: v = r.uint(4); break;
        case DW_EH_PE_udata8: v = r.uint(8); break;
        case DW_EH_PE_sleb128: v = static_cast<uint64_t>(r.sleb()); break;
        case DW_EH_PE_sdata2: v = static_cast<uint64_t>(r.sint(2)); break;
        case DW_EH_PE_sdata4: v = static_cast<uint64_t>(r.sint(4)); break;
        case DW_EH_PE_sdata8: v = static_cast<uint64_t>(r.sint(8)); break;
        default: fail("unsupported DW_EH_PE format");
    }
    if (!apply) return v;
    switch (enc & 0x70) {
        case 0x00: break;
        case DW_EH_PE_pcrel: v += field_vaddr; break;
        default: fail("unsupported DW_EH_PE application (textrel, datarel, funcrel, aligned)");
    }
    require((enc & DW_EH_PE_indirect) == 0, "indirect pointer in an FDE address");
    return v;
}

struct CfiTable {
    std::map<size_t, Cie> cies;  // keyed by offset inside .eh_frame
    std::vector<Fde> fdes;

    const Fde* find(uint64_t link_pc) const {
        for (const Fde& f : fdes)
            if (link_pc >= f.pc_begin && link_pc < f.pc_begin + f.pc_range) return &f;
        return nullptr;
    }
};

Cie parse_cie(Reader& r, const uint8_t* end, uint64_t vaddr) {
    Cie c;
    c.version = r.u8();
    require(c.version == 1 || c.version == 3 || c.version == 4, "unsupported CIE version");
    c.augmentation = r.cstr();
    if (c.version == 4) {
        require(r.u8() == 8 && r.u8() == 0, "unsupported CIE address or segment size");
    }
    c.code_align = r.uleb();
    c.data_align = r.sleb();
    c.ra_reg = c.version == 1 ? r.u8() : r.uleb();
    require(c.ra_reg < static_cast<uint64_t>(kMaxCfiReg), "CIE return-address column out of range");
    const std::string& aug = c.augmentation;
    if (!aug.empty()) {
        require(aug[0] == 'z', "CIE augmentation without 'z' cannot be skipped safely");
        c.has_z = true;
        const uint64_t len = r.uleb();
        require(len <= static_cast<uint64_t>(end - r.cur()), "CIE augmentation data out of range");
        const uint8_t* aug_end = r.cur() + len;
        for (size_t i = 1; i < aug.size(); ++i) {
            switch (aug[i]) {
                case 'R': c.fde_encoding = r.u8(); break;
                case 'L': r.u8(); break;  // LSDA encoding: the debugger does not need the LSDA
                case 'P': {
                    const uint8_t enc = r.u8();
                    read_encoded(r, enc, vaddr, /*apply=*/false);
                    break;
                }
                default: break;  // 'S' signal frame, 'B' AArch64 B-key, 'G' MTE: no data bytes
            }
        }
        r.seek(aug_end);  // 'z' gives the length, so unknown letters cannot desynchronize us
    }
    c.insns = r.cur();
    c.insns_end = end;
    return c;
}

CfiTable parse_eh_frame(const ElfImage& img) {
    CfiTable t;
    const uint8_t* base = img.eh_frame;
    const uint8_t* end = base + img.eh_frame_size;
    Reader r(base, end);
    struct PendingFde {
        size_t cie_offset;
        const uint8_t* body;
        const uint8_t* entry_end;
    };
    std::vector<PendingFde> pending;
    while (!r.done()) {
        uint64_t length = r.uint(4);
        if (length == 0) break;  // zero terminator
        require(length != 0xffffffffu, "64-bit .eh_frame entries are not supported");
        const uint8_t* id_field = r.cur();
        require(length <= static_cast<uint64_t>(end - id_field), ".eh_frame entry out of range");
        const uint8_t* entry_end = id_field + length;
        const uint32_t id = static_cast<uint32_t>(r.uint(4));
        if (id == 0) {
            t.cies[static_cast<size_t>(id_field - 4 - base)] = parse_cie(r, entry_end, img.eh_frame_vaddr);
        } else {
            // The CIE pointer counts back from the CIE pointer field itself.
            require(id <= static_cast<uint64_t>(id_field - base), "FDE CIE pointer out of range");
            pending.push_back({static_cast<size_t>(id_field - base) - id, r.cur(), entry_end});
        }
        r.seek(entry_end);
    }
    for (const PendingFde& p : pending) {
        auto it = t.cies.find(p.cie_offset);
        require(it != t.cies.end(), "FDE points at a missing CIE");
        const Cie& c = it->second;
        Reader fr(base, p.entry_end);
        fr.seek(p.body);
        Fde f;
        f.cie_offset = p.cie_offset;
        f.pc_begin = read_encoded(fr, c.fde_encoding, img.eh_frame_vaddr);
        f.pc_range = read_encoded(fr, c.fde_encoding & 0x0f, img.eh_frame_vaddr);  // a size: no pcrel
        if (c.has_z) fr.skip(fr.uleb());
        f.insns = fr.cur();
        f.insns_end = p.entry_end;
        t.fdes.push_back(f);
    }
    return t;
}

// ---------------------------------------------------------------------------------------------
// The CFI row machine (DWARF 5 sections 6.4.2 and 6.4.3).
//
// Unset means "no instruction mentioned this column". DWARF 5 6.4.1 says the default is
// undefined, but AArch64 CIEs never mention x30, so a strict reading would end every backtrace
// at a leaf function. Like libgcc (uw_update_context) this unwinder treats Unset as same value
// and only an explicit DW_CFA_undefined on the return-address column as "outermost frame".

enum class RuleKind { Unset, Undefined, SameValue, Offset, ValOffset, Register, Expression, ValExpression };

struct Rule {
    RuleKind kind = RuleKind::Unset;
    int64_t value = 0;  // offset from CFA, or a register number
};


struct Row {
    uint64_t cfa_reg = 0;
    int64_t cfa_offset = 0;
    bool cfa_is_expression = false;
    std::array<Rule, kMaxCfiReg> rules{};
    bool ra_signed = false;  // AArch64 RA_SIGN_STATE, toggled by DW_CFA_AARCH64_negate_ra_state
};

std::string format_cfa(const Row& row) {
    if (row.cfa_is_expression) return "CFA=<expression>";
    std::string s = "CFA=" + reg_name(row.cfa_reg);
    if (row.cfa_offset >= 0) return s + "+" + std::to_string(row.cfa_offset);
    return s + std::to_string(row.cfa_offset);
}

std::string format_rule(const Rule& rule, uint64_t reg) {
    switch (rule.kind) {
        case RuleKind::Unset: return reg_name(reg) + " (unset: same value)";
        case RuleKind::SameValue: return reg_name(reg) + " (same value)";
        case RuleKind::Undefined: return "undefined";
        case RuleKind::Offset:
            return "[CFA" + std::string(rule.value >= 0 ? "+" : "") + std::to_string(rule.value) + "]";
        case RuleKind::ValOffset: return "CFA" + std::string(rule.value >= 0 ? "+" : "") + std::to_string(rule.value);
        case RuleKind::Register: return reg_name(static_cast<uint64_t>(rule.value));
        case RuleKind::Expression: return "<expression>";
        case RuleKind::ValExpression: return "<val_expression>";
    }
    return "?";
}

class CfiMachine {
   public:
    CfiMachine(const Cie& cie, uint64_t fde_pc_begin, uint64_t target, const uint8_t* section_base, uint64_t eh_vaddr)
        : cie_(cie), loc_(fde_pc_begin), target_(target), section_base_(section_base), eh_vaddr_(eh_vaddr) {}

    // Runs one instruction stream; returns false once a location advance passes the target.
    bool run(const uint8_t* begin, const uint8_t* end, Row& row, const Row& initial) {
        Reader r(begin, end);
        // Link-time address of `begin`, so a pcrel DW_CFA_set_loc operand resolves like an FDE pc_begin.
        const uint64_t stream_vaddr = eh_vaddr_ + static_cast<uint64_t>(begin - section_base_);
        while (!r.done()) {
            const uint8_t op = r.u8();
            const uint8_t hi = op & 0xc0;
            const uint8_t lo = op & 0x3f;
            if (hi == 0x40) {  // DW_CFA_advance_loc
                if (!advance(lo * cie_.code_align)) return false;
                continue;
            }
            if (hi == 0x80) {  // DW_CFA_offset
                set(row, lo, RuleKind::Offset, static_cast<int64_t>(r.uleb()) * cie_.data_align);
                continue;
            }
            if (hi == 0xc0) {  // DW_CFA_restore
                restore(row, initial, lo);
                continue;
            }
            switch (op) {
                case 0x00: break;  // DW_CFA_nop
                case 0x01: {       // DW_CFA_set_loc
                    const uint64_t to = read_encoded(r, cie_.fde_encoding, stream_vaddr);
                    if (to > target_) return false;
                    loc_ = to;
                    break;
                }
                case 0x02: if (!advance(r.uint(1) * cie_.code_align)) return false; break;
                case 0x03: if (!advance(r.uint(2) * cie_.code_align)) return false; break;
                case 0x04: if (!advance(r.uint(4) * cie_.code_align)) return false; break;
                case 0x05: { const uint64_t g = r.uleb(); set(row, g, RuleKind::Offset, static_cast<int64_t>(r.uleb()) * cie_.data_align); break; }
                case 0x06: restore(row, initial, r.uleb()); break;
                case 0x07: set(row, r.uleb(), RuleKind::Undefined, 0); break;
                case 0x08: set(row, r.uleb(), RuleKind::SameValue, 0); break;
                case 0x09: { const uint64_t g = r.uleb(); set(row, g, RuleKind::Register, static_cast<int64_t>(r.uleb())); break; }
                case 0x0a: stack_.push_back(row); break;  // DW_CFA_remember_state (CFA included, as libgcc does)
                case 0x0b: {                              // DW_CFA_restore_state
                    require(!stack_.empty(), "DW_CFA_restore_state with an empty stack");
                    row = stack_.back();
                    stack_.pop_back();
                    break;
                }
                case 0x0c: row.cfa_reg = r.uleb(); row.cfa_offset = static_cast<int64_t>(r.uleb()); row.cfa_is_expression = false; break;
                case 0x0d: row.cfa_reg = r.uleb(); row.cfa_is_expression = false; break;
                case 0x0e: row.cfa_offset = static_cast<int64_t>(r.uleb()); break;
                case 0x0f: r.skip(r.uleb()); row.cfa_is_expression = true; break;
                case 0x10: { const uint64_t g = r.uleb(); r.skip(r.uleb()); set(row, g, RuleKind::Expression, 0); break; }
                case 0x11: { const uint64_t g = r.uleb(); set(row, g, RuleKind::Offset, r.sleb() * cie_.data_align); break; }
                case 0x12: row.cfa_reg = r.uleb(); row.cfa_offset = r.sleb() * cie_.data_align; row.cfa_is_expression = false; break;
                case 0x13: row.cfa_offset = r.sleb() * cie_.data_align; break;
                case 0x14: { const uint64_t g = r.uleb(); set(row, g, RuleKind::ValOffset, static_cast<int64_t>(r.uleb()) * cie_.data_align); break; }
                case 0x15: { const uint64_t g = r.uleb(); set(row, g, RuleKind::ValOffset, r.sleb() * cie_.data_align); break; }
                case 0x16: { const uint64_t g = r.uleb(); r.skip(r.uleb()); set(row, g, RuleKind::ValExpression, 0); break; }
#if defined(__aarch64__)
                case 0x2d: row.ra_signed = !row.ra_signed; break;  // DW_CFA_AARCH64_negate_ra_state
#endif
                case 0x2e: r.uleb(); break;  // DW_CFA_GNU_args_size: only matters for landing pads
                case 0x2f: { const uint64_t g = r.uleb(); set(row, g, RuleKind::Offset, -static_cast<int64_t>(r.uleb()) * cie_.data_align); break; }
                default: {
                    char buf[64];
                    std::snprintf(buf, sizeof buf, "unsupported call frame instruction 0x%02x", op);
                    fail(buf);
                }
            }
        }
        return true;
    }

   private:
    bool advance(uint64_t delta) {
        if (loc_ + delta > target_) return false;  // DWARF 5 6.4.3 step 3: stop once L2 > L1
        loc_ += delta;
        return true;
    }
    static void set(Row& row, uint64_t reg, RuleKind kind, int64_t value) {
        if (reg < static_cast<uint64_t>(kMaxCfiReg)) row.rules[reg] = {kind, value};
    }
    static void restore(Row& row, const Row& initial, uint64_t reg) {
        if (reg < static_cast<uint64_t>(kMaxCfiReg)) row.rules[reg] = initial.rules[reg];
    }

    const Cie& cie_;
    uint64_t loc_;
    uint64_t target_;
    const uint8_t* section_base_;
    uint64_t eh_vaddr_;
    std::vector<Row> stack_;
};

Row compute_row(const ElfImage& img, const CfiTable& t, const Fde& f, uint64_t link_pc) {
    const Cie& cie = t.cies.at(f.cie_offset);
    CfiMachine m(cie, f.pc_begin, link_pc, img.eh_frame, img.eh_frame_vaddr);
    Row row;
    const Row empty;
    m.run(cie.insns, cie.insns_end, row, empty);  // CIE initial instructions
    const Row initial = row;                       // what DW_CFA_restore returns to
    m.run(f.insns, f.insns_end, row, initial);
    return row;
}

// ---------------------------------------------------------------------------------------------
// The tracee: thin wrappers that turn every ptrace failure into a fatal error.

struct Mapping {
    uint64_t start = 0, end = 0, offset = 0;
    std::string perms, path;
};

std::vector<Mapping> read_maps(pid_t pid) {
    std::ifstream in("/proc/" + std::to_string(pid) + "/maps");
    if (!in) fail_errno("open /proc/PID/maps");
    std::vector<Mapping> out;
    std::string line;
    while (std::getline(in, line)) {
        Mapping m;
        char perms[8] = {};
        unsigned long long start = 0, end = 0, off = 0;
        int consumed = 0;
        if (std::sscanf(line.c_str(), "%llx-%llx %7s %llx %*s %*s%n", &start, &end, perms, &off, &consumed) < 4) continue;
        m.start = start;
        m.end = end;
        m.offset = off;
        m.perms = perms;
        std::string rest = line.substr(static_cast<size_t>(consumed));
        const size_t p = rest.find_first_not_of(' ');
        m.path = p == std::string::npos ? "" : rest.substr(p);
        out.push_back(m);
    }
    return out;
}

const Mapping* mapping_for(const std::vector<Mapping>& maps, uint64_t addr) {
    for (const Mapping& m : maps)
        if (addr >= m.start && addr < m.end) return &m;
    return nullptr;
}

std::string basename_of(const std::string& path) {
    const size_t p = path.rfind('/');
    return p == std::string::npos ? path : path.substr(p + 1);
}

uint64_t read_auxv(pid_t pid, uint64_t type) {
    std::ifstream in("/proc/" + std::to_string(pid) + "/auxv", std::ios::binary);
    if (!in) fail_errno("open /proc/PID/auxv");
    uint64_t pair[2];
    while (in.read(reinterpret_cast<char*>(pair), sizeof pair)) {
        if (pair[0] == type) return pair[1];
        if (pair[0] == AT_NULL) break;
    }
    fail("auxv entry not found");
}

std::string tracer_pid_of(pid_t pid) {
    std::ifstream in("/proc/" + std::to_string(pid) + "/status");
    std::string line;
    while (std::getline(in, line))
        if (line.rfind("TracerPid:", 0) == 0) {
            const size_t p = line.find_first_not_of(" \t", 10);
            return p == std::string::npos ? "" : line.substr(p);
        }
    fail("no TracerPid line in /proc/PID/status");
}

class Tracee {
   public:
    explicit Tracee(pid_t pid) : pid_(pid) {}
    pid_t pid() const { return pid_; }

    uint64_t peek(uint64_t addr) const {
        errno = 0;  // PTRACE_PEEKDATA returns the word, so -1 is a valid value: check errno
        const long v = ptrace(PTRACE_PEEKDATA, pid_, reinterpret_cast<void*>(addr), nullptr);
        if (v == -1 && errno != 0) fail_errno("PTRACE_PEEKDATA");
        return static_cast<uint64_t>(v);
    }
    void poke(uint64_t addr, uint64_t word) const {
        if (ptrace(PTRACE_POKEDATA, pid_, reinterpret_cast<void*>(addr), reinterpret_cast<void*>(word)) == -1)
            fail_errno("PTRACE_POKEDATA");
    }
    RawRegs regs() const {
        RawRegs r{};
        iovec iov{&r, sizeof r};
        if (ptrace(PTRACE_GETREGSET, pid_, reinterpret_cast<void*>(NT_PRSTATUS), &iov) == -1)
            fail_errno("PTRACE_GETREGSET NT_PRSTATUS");
        require(iov.iov_len == sizeof r, "short NT_PRSTATUS regset");
        return r;
    }
    void set_regs(const RawRegs& r) const {
        RawRegs copy = r;
        iovec iov{&copy, sizeof copy};
        if (ptrace(PTRACE_SETREGSET, pid_, reinterpret_cast<void*>(NT_PRSTATUS), &iov) == -1)
            fail_errno("PTRACE_SETREGSET NT_PRSTATUS");
    }
    siginfo_t siginfo() const {
        siginfo_t si{};
        if (ptrace(PTRACE_GETSIGINFO, pid_, nullptr, &si) == -1) fail_errno("PTRACE_GETSIGINFO");
        return si;
    }
    void cont(int sig = 0) const {
        std::fflush(stdout);
        if (ptrace(PTRACE_CONT, pid_, nullptr, reinterpret_cast<void*>(static_cast<long>(sig))) == -1)
            fail_errno("PTRACE_CONT");
    }
    void step() const {
        if (ptrace(PTRACE_SINGLESTEP, pid_, nullptr, nullptr) == -1) fail_errno("PTRACE_SINGLESTEP");
    }
    int wait() const {
        int status = 0;
        if (waitpid(pid_, &status, 0) == -1) fail_errno("waitpid");
        return status;
    }
    // Wait for a SIGTRAP stop; any other stop or an exit is a failure of the scenario.
    void wait_trap(const char* what) const {
        const int st = wait();
        if (!WIFSTOPPED(st) || WSTOPSIG(st) != SIGTRAP) {
            char buf[128];
            std::snprintf(buf, sizeof buf, "%s: expected a SIGTRAP stop, got status 0x%x", what, st);
            fail(buf);
        }
    }

   private:
    pid_t pid_;
};

const char* trap_code_name(int code) {
    switch (code) {
        case TRAP_BRKPT: return "TRAP_BRKPT";
        case TRAP_TRACE: return "TRAP_TRACE";
        case SI_KERNEL: return "SI_KERNEL";
        default: return "other";
    }
}

// A software breakpoint: save the original bytes, write the trap instruction over them.
class Breakpoint {
   public:
    Breakpoint(const Tracee& t, uint64_t addr) : t_(t), addr_(addr) {}
    void enable() {
        saved_ = t_.peek(addr_);
        const uint64_t mask = (1ull << (8 * kTrapLen)) - 1;  // 0xff or 0xffffffff
        t_.poke(addr_, (saved_ & ~mask) | kTrapBytes);
        enabled_ = true;
    }
    void disable() {
        require(enabled_, "disable of a breakpoint that is not enabled");
        // Restore only our bytes: another thread or breakpoint may share the rest of the word.
        const uint64_t mask = (1ull << (8 * kTrapLen)) - 1;
        const uint64_t now = t_.peek(addr_);
        t_.poke(addr_, (now & ~mask) | (saved_ & mask));
        enabled_ = false;
    }
    bool trap_in_memory() const {
        const uint64_t mask = (1ull << (8 * kTrapLen)) - 1;
        return (t_.peek(addr_) & mask) == kTrapBytes;
    }
    uint64_t addr() const { return addr_; }

   private:
    const Tracee& t_;
    uint64_t addr_;
    uint64_t saved_ = 0;
    bool enabled_ = false;
};

// ---------------------------------------------------------------------------------------------
// Unwinders.

struct Unwinder {
    const ElfImage& img;
    const CfiTable& cfi;
    const Tracee& t;
    uint64_t bias;
    std::vector<Mapping> maps;
    std::string exe_path;
    uint64_t pac_insn_mask = 0;  // AArch64 bits holding a pointer authentication code, 0 if none

    bool in_exe(uint64_t addr) const {
        const Mapping* m = mapping_for(maps, addr);
        return m && m->path == exe_path;
    }
    std::string name_at(uint64_t link_addr) const {
        const Symbol* s = img.function_at(link_addr);
        return s ? s->name : "??";
    }
    std::string where(uint64_t addr) const {
        const Mapping* m = mapping_for(maps, addr);
        if (!m) return "an unmapped address";
        return m->path.empty() ? "an anonymous mapping" : basename_of(m->path);
    }

    struct Result {
        std::vector<std::string> names;
        std::vector<std::string> rules;
        std::string stop_reason;
    };

    Result cfi_backtrace(const RawRegs& raw) const {
        Result res;
        std::array<uint64_t, kNumRegs> regs = to_dwarf(raw);
        uint64_t pc = raw_pc(raw);
        for (int depth = 0; depth < 64; ++depth) {
            if (!in_exe(pc)) {
                res.stop_reason = "return address is in " + where(pc) + ", which has no CFI loaded here";
                return res;
            }
            // A return address may point past the end of the caller (a call to a noreturn function),
            // so callers are looked up at pc - 1, inside the call instruction (DWARF 5 6.4.4).
            const uint64_t link_pc = pc - bias - (depth == 0 ? 0 : 1);
            res.names.push_back(name_at(link_pc));
            const Fde* f = cfi.find(link_pc);
            if (!f) {
                res.stop_reason = "no FDE covers the pc";
                res.rules.push_back("-");
                return res;
            }
            const Cie& cie = cfi.cies.at(f->cie_offset);
            const Row row = compute_row(img, cfi, *f, link_pc);
            const Rule ra_rule = row.rules[cie.ra_reg];
            char cfa_col[32];
            std::snprintf(cfa_col, sizeof cfa_col, "%-11s", format_cfa(row).c_str());
            res.rules.push_back(std::string(cfa_col) + " RA=" + format_rule(ra_rule, cie.ra_reg) +
                                (row.ra_signed ? " (signed)" : ""));
            if (ra_rule.kind == RuleKind::Undefined) {
                res.stop_reason = "return address rule is undefined: outermost frame";
                return res;
            }
            require(!row.cfa_is_expression, "CFA expressions are not supported");
            require(row.cfa_reg < static_cast<uint64_t>(kNumRegs), "CFA register out of range");
            const uint64_t cfa = regs[row.cfa_reg] + static_cast<uint64_t>(row.cfa_offset);
            std::array<uint64_t, kNumRegs> caller = regs;
            uint64_t ra = 0;
            bool ra_set = false;
            for (int reg = 0; reg < kMaxCfiReg; ++reg) {
                const Rule& rule = row.rules[reg];
                uint64_t v = 0;
                switch (rule.kind) {
                    case RuleKind::Unset:
                    case RuleKind::SameValue:
                    case RuleKind::Undefined:
                        continue;
                    case RuleKind::Offset: v = t.peek(cfa + static_cast<uint64_t>(rule.value)); break;
                    case RuleKind::ValOffset: v = cfa + static_cast<uint64_t>(rule.value); break;
                    case RuleKind::Register:
                        require(rule.value >= 0 && rule.value < kNumRegs, "register rule out of range");
                        v = regs[static_cast<size_t>(rule.value)];
                        break;
                    case RuleKind::Expression:
                    case RuleKind::ValExpression:
                        fail("DWARF expression rules are not supported");
                }
                if (static_cast<uint64_t>(reg) == cie.ra_reg) {
                    ra = v;
                    ra_set = true;
                }
                if (reg < kNumRegs) caller[static_cast<size_t>(reg)] = v;
            }
            if (!ra_set) {
                // Unset or same value: the return address is still in its register (AArch64 x30 in a leaf).
                require(cie.ra_reg < static_cast<uint64_t>(kNumRegs), "return address column is not a register");
                ra = regs[cie.ra_reg];
            }
            if (row.ra_signed) ra &= ~pac_insn_mask;  // strip the PAC; mask is 0 on CPUs without it
            caller[kSpReg] = cfa;                       // by definition the caller's sp at the call is the CFA
            regs = caller;
            pc = ra;
        }
        res.stop_reason = "depth limit";
        return res;
    }

    // The frame-pointer walk most profilers use: [fp] is the caller's fp, [fp + 8] the return address.
    std::vector<std::string> fp_backtrace(const RawRegs& raw) const {
        std::vector<std::string> names;
        const std::array<uint64_t, kNumRegs> regs = to_dwarf(raw);
        names.push_back(name_at(raw_pc(raw) - bias));
        uint64_t fp = regs[kFpReg];
        for (int depth = 0; depth < 64 && fp != 0 && (fp & 7) == 0; ++depth) {
            const uint64_t ra = t.peek(fp + 8) & ~pac_insn_mask;
            const uint64_t next = t.peek(fp);
            if (!in_exe(ra)) break;
            names.push_back(name_at(ra - bias - 1));
            if (next <= fp) break;  // the stack grows down, so callers' frames are at higher addresses
            fp = next;
        }
        return names;
    }
};

std::string join(const std::vector<std::string>& v) {
    std::string s;
    for (size_t i = 0; i < v.size(); ++i) s += (i ? " " : "") + v[i];
    return s;
}

std::string real_path(const std::string& p) {
    char buf[PATH_MAX];
    if (!realpath(p.c_str(), buf)) fail_errno("realpath " + p);
    return buf;
}

uint64_t pac_mask_of(const Tracee& t) {
#if defined(__aarch64__)
    struct {
        uint64_t data_mask, insn_mask;
    } mask{};
    iovec iov{&mask, sizeof mask};
    // The regset exists only when the CPU has pointer authentication (HWCAP_PACA).
    if (ptrace(PTRACE_GETREGSET, t.pid(), reinterpret_cast<void*>(NT_ARM_PAC_MASK), &iov) == 0) return mask.insn_mask;
#else
    (void)t;
#endif
    return 0;
}

pid_t spawn(const std::string& path, const std::vector<std::string>& args, bool traced) {
    std::fflush(stdout);
    const pid_t pid = fork();
    if (pid == -1) fail_errno("fork");
    if (pid == 0) {
        const int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) dup2(devnull, STDOUT_FILENO);  // keep the target's printf out of our output
        if (traced && ptrace(PTRACE_TRACEME, 0, nullptr, nullptr) == -1) _exit(126);
        std::vector<char*> argv;
        argv.push_back(const_cast<char*>(path.c_str()));
        for (const std::string& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        execv(path.c_str(), argv.data());
        _exit(127);
    }
    return pid;
}

int native_exit_status(const std::string& path) {
    const pid_t pid = spawn(path, {}, false);
    int st = 0;
    if (waitpid(pid, &st, 0) == -1) fail_errno("waitpid native run");
    require(WIFEXITED(st), "native run did not exit normally");
    return WEXITSTATUS(st);
}

struct Session {
    std::string path;
    ElfImage img;
    CfiTable cfi;
    pid_t pid = 0;
    uint64_t bias = 0;
};

// fork, PTRACE_TRACEME, execv, and stop at the exec SIGTRAP; then compute the load bias.
Session launch(const std::string& target, bool verbose) {
    Session s;
    s.path = real_path(target);
    s.img = load_elf(s.path);
    s.cfi = parse_eh_frame(s.img);
    s.pid = spawn(s.path, {}, true);
    Tracee t(s.pid);
    const int st = t.wait();
    require(WIFSTOPPED(st) && WSTOPSIG(st) == SIGTRAP, "tracee did not stop with SIGTRAP after execve");
    if (ptrace(PTRACE_SETOPTIONS, s.pid, nullptr, reinterpret_cast<void*>(PTRACE_O_EXITKILL)) == -1)
        fail_errno("PTRACE_SETOPTIONS");
    const uint64_t at_entry = read_auxv(s.pid, AT_ENTRY);
    s.bias = at_entry - s.img.entry;
    if (!verbose) return s;

    std::printf("=== Launch (fork, PTRACE_TRACEME, execv) ===\n");
    std::printf("Architecture: %s\n", kArchName);
    std::printf("Stop after execve: SIGTRAP, waitpid status 0x%x\n", st);
    std::printf("ELF type: %s\n", s.img.type == ET_DYN ? "ET_DYN (position-independent)" : "ET_EXEC");
    std::printf("Debug info in target: %s\n", s.img.has_debug_info ? ".debug_info present" : "none (.eh_frame and .symtab only)");
    const std::vector<Mapping> maps = read_maps(s.pid);
    uint64_t first = 0;
    for (const Mapping& m : maps)
        if (m.path == s.path && m.offset == 0) {
            first = m.start;
            break;
        }
    std::printf("Load bias (AT_ENTRY - e_entry) equals start of first target mapping: %s\n",
                first != 0 && first == s.bias ? "yes" : "no");
    std::printf("CIEs: %zu, FDEs: %zu\n", s.cfi.cies.size(), s.cfi.fdes.size());
    require(!s.cfi.cies.empty(), ".eh_frame has no CIE");
    const Cie& cie = s.cfi.cies.begin()->second;
    std::printf("First CIE: version %u, augmentation \"%s\", code_align %" PRIu64 ", data_align %" PRId64
                ", return address column %s\n",
                cie.version, cie.augmentation.c_str(), cie.code_align, cie.data_align, reg_name(cie.ra_reg).c_str());
    return s;
}

uint64_t symbol_addr(const Session& s, const std::string& name) {
    const std::optional<Symbol> sym = s.img.find(name);
    require(sym.has_value(), "symbol not found: " + name);
    return s.bias + sym->value;
}

Unwinder make_unwinder(const Session& s, const Tracee& t) {
    return Unwinder{s.img, s.cfi, t, s.bias, read_maps(s.pid), s.path, pac_mask_of(t)};
}

void print_backtrace(const Unwinder::Result& r) {
    for (size_t i = 0; i < r.names.size(); ++i)
        std::printf("#%zu %-13s %s\n", i, r.names[i].c_str(), i < r.rules.size() ? r.rules[i].c_str() : "-");
    std::printf("Stop: %s\n", r.stop_reason.c_str());
}

int cmd_launch(const std::string& target) {
    const int native = native_exit_status(target);
    Session s = launch(target, true);
    Tracee t(s.pid);

    std::printf("\n=== Breakpoint ===\n");
    const uint64_t bp_addr = symbol_addr(s, "leaf_compute");
    const std::vector<Mapping> maps = read_maps(s.pid);  // must outlive `text`, which points into it
    const Mapping* text = mapping_for(maps, bp_addr);
    require(text != nullptr, "breakpoint address is not mapped");
    std::printf("Breakpoint on leaf_compute: bias + st_value, in a %s mapping of %s\n", text->perms.c_str(),
                basename_of(text->path).c_str());
    Breakpoint bp(t, bp_addr);
    bp.enable();
    std::printf("Wrote %s with PTRACE_POKEDATA; read back: %s\n", kTrapName, bp.trap_in_memory() ? "trap present" : "trap missing");

    int hits = 0;
    t.cont();
    t.wait_trap("first breakpoint hit");
    ++hits;
    siginfo_t si = t.siginfo();
    RawRegs r = t.regs();
    std::printf("Hit 1: SIGTRAP, si_code %s (%d)\n", trap_code_name(si.si_code), si.si_code);
    std::printf("pc - breakpoint address after the trap: %" PRId64 "\n", static_cast<int64_t>(raw_pc(r) - bp_addr));
    if (kPcAfterTrap != 0) {
        set_raw_pc(r, bp_addr);  // int3 already executed: move pc back onto the original instruction
        t.set_regs(r);
        std::printf("Rewound pc by %d to re-execute the original instruction\n", kPcAfterTrap);
    }
    std::printf("Arguments: a = %s = %" PRIu64 ", b = %s = %" PRIu64 "\n", kArg0Name, raw_arg(r, 0) & 0xffffffffu,
                kArg1Name, raw_arg(r, 1) & 0xffffffffu);

    std::printf("\n=== Backtrace: DWARF CFI from .eh_frame ===\n");
    const Unwinder uw = make_unwinder(s, t);
    const Unwinder::Result bt = uw.cfi_backtrace(r);
    print_backtrace(bt);
    std::printf("CFI frames: %s\n", join(bt.names).c_str());

    std::printf("\n=== Backtrace: frame-pointer walk at the same stop ===\n");
    const std::vector<std::string> fpw = uw.fp_backtrace(r);
    std::printf("FP frames: %s\n", join(fpw).c_str());
    std::printf("Frame-pointer walk equals CFI walk: %s\n", fpw == bt.names ? "yes" : "no");

    std::printf("\n=== Step over the breakpoint ===\n");
    bp.disable();
    t.step();
    t.wait_trap("single-step");
    si = t.siginfo();
    const RawRegs stepped = t.regs();
    std::printf("PTRACE_SINGLESTEP: SIGTRAP, si_code %s (%d)\n", trap_code_name(si.si_code), si.si_code);
    std::printf("pc advanced by %" PRId64 " bytes, still in %s\n", static_cast<int64_t>(raw_pc(stepped) - bp_addr),
                uw.name_at(raw_pc(stepped) - s.bias).c_str());
    bp.enable();
    std::printf("Breakpoint re-inserted: %s\n", bp.trap_in_memory() ? "yes" : "no");

    t.cont();
    t.wait_trap("second breakpoint hit");
    ++hits;
    r = t.regs();
    std::printf("Hit 2: a = %" PRIu64 ", b = %" PRIu64 "\n", raw_arg(r, 0) & 0xffffffffu, raw_arg(r, 1) & 0xffffffffu);
    if (kPcAfterTrap != 0) set_raw_pc(r, bp_addr);
    const uint64_t old_b = raw_arg(r, 1) & 0xffffffffu;
    set_raw_arg(r, 1, 0);
    t.set_regs(r);
    std::printf("PTRACE_SETREGSET: %s %" PRIu64 " -> %" PRIu64 "\n", kArg1Name, old_b, raw_arg(t.regs(), 1) & 0xffffffffu);
    bp.disable();

    t.cont();
    const int st = t.wait();
    require(WIFEXITED(st), "tracee did not exit normally");
    std::printf("\n=== Exit ===\n");
    std::printf("Breakpoint hits: %d\n", hits);
    std::printf("Native run exit status: %d\n", native);
    std::printf("Debugged run exit status: %d\n", WEXITSTATUS(st));
    return 0;
}

int cmd_unwind(const std::string& target, const std::string& offset_hex) {
    Session s = launch(target, false);
    Tracee t(s.pid);
    const uint64_t off = std::strtoull(offset_hex.c_str(), nullptr, 16);
    require(off != 0, "bad offset");
    std::printf("=== Stripped target: CFI backtrace without symbols ===\n");
    std::printf("Target has .symtab: %s\n", s.img.has_symtab ? "yes" : "no");
    std::printf("Target has .eh_frame: %s (%zu FDEs)\n", s.img.eh_frame ? "yes" : "no", s.cfi.fdes.size());
    Breakpoint bp(t, s.bias + off);
    bp.enable();
    t.cont();
    t.wait_trap("breakpoint in stripped target");
    RawRegs r = t.regs();
    set_raw_pc(r, raw_pc(r) - kPcAfterTrap);  // unwind from the breakpoint address itself
    const Unwinder uw = make_unwinder(s, t);
    const Unwinder::Result bt = uw.cfi_backtrace(r);
    std::printf("Frames unwound inside the target: %zu\n", bt.names.size());
    std::printf("Frames with a name: %zu\n",
                static_cast<size_t>(bt.names.size() - static_cast<size_t>(std::count(bt.names.begin(), bt.names.end(), "??"))));
    std::printf("Stop: %s\n", bt.stop_reason.c_str());
    kill(s.pid, SIGKILL);
    t.wait();
    return 0;
}

void sleep_ms(long ms) {
    timespec ts{ms / 1000, (ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {
    }
}

int cmd_attach(const std::string& target) {
    std::printf("=== Attach (PTRACE_SEIZE, PTRACE_INTERRUPT) ===\n");
    const std::string path = real_path(target);
    const ElfImage img = load_elf(path);
    const pid_t pid = spawn(path, {"--spin"}, false);
    // Wait until execv has replaced the forked copy of minidbg, or the auxv we read would be ours.
    const std::string exe_link = "/proc/" + std::to_string(pid) + "/exe";
    for (int i = 0;; ++i) {
        char buf[PATH_MAX] = {};
        const ssize_t n = readlink(exe_link.c_str(), buf, sizeof buf - 1);
        if (n > 0 && path == std::string(buf, static_cast<size_t>(n))) break;
        require(i < 5000, "target never reached execv");
        sleep_ms(1);
    }
    std::printf("TracerPid before attach: %s\n", tracer_pid_of(pid) == "0" ? "0 (untraced)" : "nonzero");
    if (ptrace(PTRACE_SEIZE, pid, nullptr, reinterpret_cast<void*>(PTRACE_O_EXITKILL)) == -1) fail_errno("PTRACE_SEIZE");
    std::printf("PTRACE_SEIZE: ok, TracerPid is minidbg: %s\n",
                tracer_pid_of(pid) == std::to_string(getpid()) ? "yes" : "no");
    Tracee t(pid);
    const std::optional<Symbol> counter_sym = img.find("spin_counter");
    require(counter_sym.has_value(), "spin_counter symbol missing");

    auto interrupt = [&](const char* what) {
        if (ptrace(PTRACE_INTERRUPT, pid, nullptr, nullptr) == -1) fail_errno("PTRACE_INTERRUPT");
        const int st = t.wait();
        require(WIFSTOPPED(st) && WSTOPSIG(st) == SIGTRAP && (st >> 16) == PTRACE_EVENT_STOP,
                std::string(what) + ": expected PTRACE_EVENT_STOP");
        return st;
    };
    // The first interrupt may land in the dynamic loader or in main; resume until it lands in the loop.
    const std::optional<Symbol> spin_sym = img.find("spin_forever");
    require(spin_sym.has_value(), "spin_forever symbol missing");
    int st = 0;
    uint64_t bias = 0;
    for (int attempt = 0;; ++attempt) {
        sleep_ms(10);
        st = interrupt("first interrupt");
        // Read auxv only at a ptrace stop: /proc/PID/exe switches before execve has written the new
        // auxiliary vector, while a stop means execve has returned to user space.
        if (attempt == 0) bias = read_auxv(pid, AT_ENTRY) - img.entry;
        const uint64_t link_pc = raw_pc(t.regs()) - bias;
        if (link_pc >= spin_sym->value && link_pc < spin_sym->value + spin_sym->size) break;
        require(attempt < 200, "target never reached spin_forever");
        t.cont();
    }
    std::printf("PTRACE_INTERRUPT stop: SIGTRAP with PTRACE_EVENT_STOP in status >> 16: %s, waitpid status 0x%x\n",
                (st >> 16) == PTRACE_EVENT_STOP ? "yes" : "no", st);
    const uint64_t counter = bias + counter_sym->value;
    const Symbol* fn = img.function_at(raw_pc(t.regs()) - bias);
    std::printf("Interrupted inside: %s\n", fn ? fn->name.c_str() : "??");
    const uint64_t c1 = t.peek(counter);
    sleep_ms(20);
    const uint64_t c1_again = t.peek(counter);
    std::printf("spin_counter unchanged while stopped: %s\n", c1 == c1_again ? "yes" : "no");
    t.cont();
    sleep_ms(20);
    interrupt("second interrupt");
    const uint64_t c2 = t.peek(counter);
    std::printf("spin_counter advanced while running: %s\n", c2 > c1 ? "yes" : "no");
    if (ptrace(PTRACE_DETACH, pid, nullptr, nullptr) == -1) fail_errno("PTRACE_DETACH");
    std::printf("PTRACE_DETACH: TracerPid after detach: %s\n", tracer_pid_of(pid) == "0" ? "0 (untraced, still running)" : "nonzero");
    kill(pid, SIGKILL);
    int end = 0;
    if (waitpid(pid, &end, 0) == -1) fail_errno("waitpid after kill");
    std::printf("Killed with SIGKILL: %s\n", WIFSIGNALED(end) && WTERMSIG(end) == SIGKILL ? "yes" : "no");
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IOLBF, 0);
    if (argc == 3 && std::strcmp(argv[1], "launch") == 0) return cmd_launch(argv[2]);
    if (argc == 4 && std::strcmp(argv[1], "unwind") == 0) return cmd_unwind(argv[2], argv[3]);
    if (argc == 3 && std::strcmp(argv[1], "attach") == 0) return cmd_attach(argv[2]);
    std::fprintf(stderr, "usage: minidbg launch TARGET | unwind TARGET HEX_OFFSET | attach TARGET\n");
    return 2;
}
