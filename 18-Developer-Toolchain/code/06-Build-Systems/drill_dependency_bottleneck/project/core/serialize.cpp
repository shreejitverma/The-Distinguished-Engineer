// The one translation unit that needs the generated header.
#include "core/serialize.h"
#include "core/schema.h"
namespace core {
int serialized_field_count() { return kOrderFieldCount; }
}  // namespace core
