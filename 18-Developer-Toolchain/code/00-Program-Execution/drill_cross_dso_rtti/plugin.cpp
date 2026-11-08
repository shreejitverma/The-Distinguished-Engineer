#include "error.hpp"
#include <typeinfo>

extern "C" __attribute__((visibility("default"))) const std::type_info* get_plugin_typeinfo() {
    return &typeid(PluginError);
}

extern "C" __attribute__((visibility("default"))) void trigger_error() {
    throw PluginError();
}
