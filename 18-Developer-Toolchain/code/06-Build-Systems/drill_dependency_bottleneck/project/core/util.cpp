#include "core/util.h"
namespace core {
int checksum(int seed) { return seed * 31 + 7; }
}  // namespace core
