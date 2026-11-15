#include <iostream>

extern "C" int rs_mul_len(int a, int b);

int main() {
    std::cout << "rs_mul_len(6, 7) from C++ = " << rs_mul_len(6, 7) << "\n";
    return 0;
}
