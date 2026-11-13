// widget_inst.cpp - the one TU that owns the Widget<int> instantiation.
#include "widget.hpp"

// Explicit instantiation definition: instantiates every member of Widget<int> here.
template struct Widget<int>;
