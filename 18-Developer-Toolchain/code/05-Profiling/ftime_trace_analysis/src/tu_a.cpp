// tu_a.cpp - a consumer TU: pays for heavy_meta.hpp and uses Widget<int>.
#include "cheap.hpp"
#include "heavy_meta.hpp"
#include "widget.hpp"

int use_a(int x) {
    Widget<int> w{x};
    return cheap::add(w.crunch(), w.fast()) + static_cast<int>(meta::kListSize);
}
