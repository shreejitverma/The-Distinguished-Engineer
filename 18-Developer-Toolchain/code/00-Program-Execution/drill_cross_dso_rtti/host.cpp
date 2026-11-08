#include "error.hpp"
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <typeinfo>

#ifndef DLMODE
#define DLMODE RTLD_LOCAL
#endif

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <plugin_path> [--strict]\n", argv[0]);
        return 2;
    }

    void* handle = dlopen(argv[1], RTLD_NOW | DLMODE);
    if (!handle) {
        std::fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 2;
    }

    auto get_ti = reinterpret_cast<const std::type_info* (*)()>(dlsym(handle, "get_plugin_typeinfo"));
    auto trigger = reinterpret_cast<void (*)()>(dlsym(handle, "trigger_error"));
    if (!get_ti || !trigger) {
        std::fprintf(stderr, "dlsym failed: %s\n", dlerror());
        return 2;
    }

    const std::type_info* host_ti = &typeid(PluginError);
    const std::type_info* plugin_ti = get_ti();
    std::printf("addr_eq=%d op_eq=%d strcmp_eq=%d\n", host_ti == plugin_ti, *host_ti == *plugin_ti,
                std::strcmp(host_ti->name(), plugin_ti->name()) == 0);
    std::fflush(stdout);

    const bool strict = argc > 2 && std::strcmp(argv[2], "--strict") == 0;
    if (strict) {
        // Only a PluginError handler exists: a failed match has nowhere to go but std::terminate.
        try {
            trigger();
        } catch (const PluginError&) {
            std::puts("outcome=caught-PluginError");
        }
    } else {
        // std::exception is a base class resolved through the shared C++ runtime, so it still matches.
        try {
            trigger();
        } catch (const PluginError&) {
            std::puts("outcome=caught-PluginError");
        } catch (const std::exception&) {
            std::puts("outcome=fell-back-to-std-exception");
        }
    }
    return 0;
}
