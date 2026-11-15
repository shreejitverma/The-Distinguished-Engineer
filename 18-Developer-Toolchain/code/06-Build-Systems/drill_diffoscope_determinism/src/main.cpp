#include <cstdio>

#include "opcodes.h"

extern "C" const char* build_stamp();
int dispatch(Op op, int a, int b);
const char* op_name(unsigned i);

int main() {
    std::printf("%s\n", build_stamp());
    std::printf("%s(2, 3) = %d\n", op_name(0), dispatch(static_cast<Op>(0), 2, 3));
    return 0;
}
