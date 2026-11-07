#define USE_KEY_FUNCTION
#define ERROR_EXPORT __attribute__((visibility("default")))
#include "error.hpp"

PluginError::~PluginError() = default;
