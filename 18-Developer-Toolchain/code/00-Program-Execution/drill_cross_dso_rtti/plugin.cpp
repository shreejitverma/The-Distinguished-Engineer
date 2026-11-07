#include "error.hpp"
#include <typeinfo>

extern "C" __attribute__((visibility("default"))) const void* get_plugin_typeinfo() {
    return static_cast<const void*>(&typeid(PluginError));
}

extern "C" __attribute__((visibility("default"))) void trigger_error() {
    throw PluginError();
}
