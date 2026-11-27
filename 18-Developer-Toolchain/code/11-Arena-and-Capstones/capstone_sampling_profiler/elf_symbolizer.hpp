#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <map>

namespace profiler {

struct Symbol {
    uintptr_t vaddr;
    size_t size;
    std::string name;
};

struct ModuleMapping {
    uintptr_t start_addr;
    uintptr_t end_addr;
    uintptr_t file_offset;
    uintptr_t load_bias;
    std::string path;
    std::vector<Symbol> symbols;
};

class ElfSymbolizer {
public:
    ElfSymbolizer();
    bool init();
    std::string resolve(uintptr_t ip);

private:
    std::vector<ModuleMapping> modules_;
    bool load_module_symbols(ModuleMapping& mod);
    void parse_proc_maps();
};

} // namespace profiler
