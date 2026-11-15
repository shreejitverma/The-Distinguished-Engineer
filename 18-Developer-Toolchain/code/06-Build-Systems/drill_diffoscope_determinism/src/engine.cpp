#include <cstdio>

#include "opcodes.h"

int dispatch(Op op, int a, int b) {
    switch (op) {
        case Op::ADD: return a + b;
        case Op::SUB: return a - b;
        case Op::MUL: return a * b;
        case Op::DIV: return b != 0 ? a / b : 0;
        default: return 0;
    }
}

const char* op_name(unsigned i) {
    if (i >= sizeof(kOpNames) / sizeof(kOpNames[0])) {
        std::fprintf(stderr, "bad opcode %u at %s:%d\n", i, __FILE__, __LINE__);
        return "?";
    }
    return kOpNames[i];
}
