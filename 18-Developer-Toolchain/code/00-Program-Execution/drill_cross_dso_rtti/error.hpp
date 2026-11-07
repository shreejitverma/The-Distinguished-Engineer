#pragma once
#include <exception>

#ifndef ERROR_EXPORT
#define ERROR_EXPORT
#endif

#ifdef USE_KEY_FUNCTION
struct ERROR_EXPORT PluginError : public std::exception {
    ~PluginError() override;
    const char* what() const noexcept override {
        return "Cross-DSO PluginError: operation failed in worker thread";
    }
};
#else
struct ERROR_EXPORT PluginError : public std::exception {
    const char* what() const noexcept override {
        return "Cross-DSO PluginError: operation failed in worker thread";
    }
};
#endif
