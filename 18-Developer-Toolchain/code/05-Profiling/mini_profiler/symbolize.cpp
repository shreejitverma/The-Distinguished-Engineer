// Offline back end: raw profile -> symbol names -> folded stacks.
//
//   symbolize RAW [--exe PATH] [--lookup bounded|nearest] [--stats PATH]   folded stacks on stdout
//   symbolize --symbols IMAGE                                              function symbols of IMAGE
//
// Exit status: 0 success, 2 bad usage or unreadable input, 3 the main executable on disk is
// not the build that was sampled (its build ID or LC_UUID differs from the recorded one).
#include "image.hpp"

#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include <cxxabi.h>

namespace {

enum class Lookup { bounded, nearest };

struct Module {
    std::uint64_t start = 0, end = 0, file_offset = 0;
    std::string identity; // recorded at sample time, "-" when the front end had none
    bool is_main = false;
    std::string path;
    std::optional<image::Image> img;
};

struct Stats {
    std::uint64_t samples = 0, truncated = 0;
    std::uint64_t frames = 0, resolved = 0;           // all frames, and those that got a symbol name
    std::uint64_t main_frames = 0, main_resolved = 0; // the same, for frames inside the main executable
};

std::string basename_of(const std::string& path) {
    const std::size_t slash = path.find_last_of('/');
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

// "_ZL4burnj" -> "burn(unsigned int)". Mach-O prefixes every C-level name with one underscore.
std::string display_name(const std::string& raw, image::Format format) {
    std::string name = raw;
    if (format == image::Format::macho64 && !name.empty() && name[0] == '_') {
        name.erase(0, 1);
    }
    if (name.rfind("_Z", 0) == 0) {
        int status = 0;
        char* demangled = abi::__cxa_demangle(name.c_str(), nullptr, nullptr, &status);
        if (status == 0 && demangled != nullptr) {
            name = demangled;
        }
        std::free(demangled);
    }
    for (char& c : name) {
        if (c == ';') {
            c = ':'; // ';' separates frames in the folded format
        }
    }
    return name;
}

// Runtime address -> link-time virtual address of the image on disk.
std::optional<std::uint64_t> to_vaddr(const Module& m, std::uint64_t addr) {
    const image::Image& img = *m.img;
    if (img.format == image::Format::macho64) {
        // The map line describes __TEXT, so the distance from its start is the same in memory
        // and in the file, whatever the ASLR slide and whatever the shared cache did to fileoff.
        return img.text_vmaddr + (addr - m.start);
    }
    // ELF: mapping -> file offset -> the PT_LOAD segment that holds that offset -> p_vaddr.
    const std::uint64_t file_offset = addr - m.start + m.file_offset;
    for (const image::Segment& seg : img.segments) {
        if (file_offset >= seg.fileoff && file_offset - seg.fileoff < seg.filesize) {
            return seg.vmaddr + (file_offset - seg.fileoff);
        }
    }
    return std::nullopt;
}

std::string resolve(const std::vector<Module>& modules, std::uint64_t addr, Lookup lookup, Stats* stats) {
    ++stats->frames;
    for (const Module& m : modules) {
        if (addr < m.start || addr >= m.end) {
            continue;
        }
        const std::string unresolved = "[" + basename_of(m.path) + "]";
        stats->main_frames += m.is_main ? 1 : 0;
        if (!m.img) {
            return unresolved;
        }
        const std::optional<std::uint64_t> vaddr = to_vaddr(m, addr);
        const std::vector<image::Symbol>& symbols = m.img->symbols;
        if (!vaddr || symbols.empty()) {
            return unresolved;
        }
        // The last symbol that starts at or before the address.
        auto it = std::upper_bound(symbols.begin(), symbols.end(), *vaddr,
                                   [](std::uint64_t v, const image::Symbol& s) { return v < s.addr; });
        if (it == symbols.begin()) {
            return unresolved;
        }
        --it;
        if (lookup == Lookup::bounded) {
            // ELF records each function's size, so an address past the end is not that function.
            if (it->size != 0 && *vaddr - it->addr >= it->size) {
                return unresolved;
            }
            // Mach-O records no sizes, but LC_FUNCTION_STARTS survives stripping: if another
            // function starts between the symbol and the address, the symbol is the wrong one.
            const std::vector<std::uint64_t>& starts = m.img->function_starts;
            auto fs = std::upper_bound(starts.begin(), starts.end(), *vaddr);
            if (fs != starts.begin() && *(fs - 1) > it->addr) {
                return unresolved;
            }
        }
        ++stats->resolved;
        stats->main_resolved += m.is_main ? 1 : 0;
        return display_name(it->name, m.img->format);
    }
    return "[unknown]";
}

int dump_symbols(const std::string& path) {
    std::string error;
    const std::optional<image::Image> img = image::load(path, "-", &error);
    if (!img) {
        std::fprintf(stderr, "symbolize: %s\n", error.c_str());
        return 2;
    }
    for (const image::Symbol& s : img->symbols) {
        std::printf("%016" PRIx64 " %s\n", s.addr, s.name.c_str());
    }
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    std::string raw_path, exe_override, stats_path;
    Lookup lookup = Lookup::bounded;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        const bool has_value = i + 1 < argc;
        if (arg == "--symbols" && has_value) {
            return dump_symbols(argv[i + 1]);
        } else if (arg == "--exe" && has_value) {
            exe_override = argv[++i];
        } else if (arg == "--stats" && has_value) {
            stats_path = argv[++i];
        } else if (arg == "--lookup" && has_value) {
            const std::string value = argv[++i];
            if (value != "bounded" && value != "nearest") {
                std::fprintf(stderr, "symbolize: --lookup takes bounded or nearest\n");
                return 2;
            }
            lookup = value == "nearest" ? Lookup::nearest : Lookup::bounded;
        } else if (raw_path.empty() && arg.rfind("--", 0) != 0) {
            raw_path = arg;
        } else {
            std::fprintf(stderr, "usage: symbolize RAW [--exe PATH] [--lookup bounded|nearest] [--stats PATH]\n");
            return 2;
        }
    }
    std::ifstream raw(raw_path);
    std::string line;
    if (raw_path.empty() || !raw || !std::getline(raw, line) || line != "miniprof-raw 1") {
        std::fprintf(stderr, "symbolize: %s is not a miniprof-raw 1 file\n", raw_path.c_str());
        return 2;
    }

    std::vector<Module> modules;
    std::vector<std::vector<std::uint64_t>> samples;
    Stats stats;
    std::string main_format = "none", main_source = "none", main_identity = "unrecorded";
    std::size_t main_symbols = 0;
    while (std::getline(raw, line)) {
        std::istringstream in(line);
        std::string kind;
        in >> kind;
        if (kind == "map") {
            Module m;
            std::string role;
            in >> std::hex >> m.start >> m.end >> m.file_offset >> m.identity >> role;
            in >> std::ws;
            std::getline(in, m.path); // the rest of the line: a path may contain spaces
            m.is_main = role == "main";
            const std::string file = m.is_main && !exe_override.empty() ? exe_override : m.path;
            std::string error;
            m.img = image::load(file, m.identity, &error);
            if (m.img && m.identity != "-" && m.img->identity != m.identity) {
                // Wrong build: its symbol table describes different code at the same addresses.
                if (m.is_main) {
                    std::fprintf(stderr, "symbolize: identity mismatch for %s: sampled %s, file has %s\n", file.c_str(),
                                 m.identity.c_str(), m.img->identity.c_str());
                    return 3;
                }
                m.img.reset();
            }
            if (m.is_main && m.img) {
                main_format = m.img->format == image::Format::elf64 ? "ELF64" : "Mach-O 64";
                main_source = m.img->symbol_source;
                main_symbols = m.img->symbols.size();
                main_identity = m.identity == "-" ? "unrecorded" : "match";
            } else if (m.is_main) {
                std::fprintf(stderr, "symbolize: %s\n", error.c_str());
                return 2;
            }
            modules.push_back(std::move(m));
        } else if (kind == "sample") {
            unsigned truncated = 0;
            in >> truncated;
            stats.truncated += truncated;
            std::vector<std::uint64_t> pcs;
            std::uint64_t pc = 0;
            while (in >> std::hex >> pc) {
                pcs.push_back(pc);
            }
            if (!pcs.empty()) {
                samples.push_back(std::move(pcs));
            }
        }
    }

    // Fold: one line per distinct stack, root first, frames joined by ';', then the sample count.
    std::map<std::string, std::uint64_t> folded;
    for (const std::vector<std::uint64_t>& pcs : samples) {
        ++stats.samples;
        std::string stack;
        for (std::size_t i = pcs.size(); i-- > 0;) {
            // pcs[0] is the interrupted pc. Every other entry is a return address, which points
            // one instruction past the call; look up the byte before it to stay inside the caller.
            const std::uint64_t addr = i == 0 ? pcs[i] : pcs[i] - 1;
            if (!stack.empty()) {
                stack.push_back(';');
            }
            stack += resolve(modules, addr, lookup, &stats);
        }
        ++folded[stack];
    }
    for (const auto& [stack, count] : folded) {
        std::printf("%s %" PRIu64 "\n", stack.c_str(), count);
    }

    if (!stats_path.empty()) {
        std::FILE* out = std::fopen(stats_path.c_str(), "w");
        if (out == nullptr) {
            std::fprintf(stderr, "symbolize: cannot write %s\n", stats_path.c_str());
            return 2;
        }
        std::fprintf(out, "format=%s\nsymbol_source=%s\nsymbols=%zu\nidentity=%s\n", main_format.c_str(),
                     main_source.c_str(), main_symbols, main_identity.c_str());
        std::fprintf(out, "samples=%" PRIu64 "\ntruncated=%" PRIu64 "\nframes=%" PRIu64 "\nresolved=%" PRIu64 "\n",
                     stats.samples, stats.truncated, stats.frames, stats.resolved);
        std::fprintf(out, "main_frames=%" PRIu64 "\nmain_resolved=%" PRIu64 "\n", stats.main_frames,
                     stats.main_resolved);
        if (std::fclose(out) != 0) {
            std::fprintf(stderr, "symbolize: cannot write %s\n", stats_path.c_str());
            return 2;
        }
    }
    return 0;
}
