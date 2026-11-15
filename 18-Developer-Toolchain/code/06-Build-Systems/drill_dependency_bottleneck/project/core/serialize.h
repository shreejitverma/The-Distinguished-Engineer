// The only public face of the generated schema: a function, not the struct.
// Callers include this header, which does not include core/schema.h.
#pragma once
namespace core {
int serialized_field_count();
}  // namespace core
