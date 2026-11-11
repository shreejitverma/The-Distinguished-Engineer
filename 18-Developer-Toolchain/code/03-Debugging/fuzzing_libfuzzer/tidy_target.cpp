// tidy_target.cpp - AST-level defects for clang-tidy.
#include <cstring>
#include <string>
#include <utility>
#include <vector>

// bugprone-use-after-move: s is read after its contents were moved into v.
size_t queue_name(std::vector<std::string> &v, std::string s) {
    v.push_back(std::move(s));
    return s.size();
}

// clang-diagnostic-sizeof-pointer-memaccess: sizeof of a pointer, not of the buffer it points to.
void clear_quote(char *quote) {
    std::memset(quote, 0, sizeof(quote));
}

// clang-analyzer-core.DivideZero: reported only when clang-analyzer-* checks are enabled.
int per_lot(int qty, int lots) {
    if (lots == 0) return qty / lots;
    return qty / lots;
}
