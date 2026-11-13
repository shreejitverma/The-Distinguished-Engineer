// widget.hpp - a class template used by several translation units.
// crunch() is defined out of class, so it is not inline; fast() is defined in
// the class body, so it is implicitly inline. The difference matters for
// `extern template`: an explicit instantiation declaration suppresses the
// implicit instantiation of crunch() in every TU that sees it, but the compiler
// may still instantiate fast() so that it can inline it.
#pragma once

template <class T>
struct Widget {
    T seed;
    T crunch() const;
    T fast() const { return seed * 3 + 1; }
};

template <class T>
T Widget<T>::crunch() const {
    T acc = seed;
    for (int i = 0; i < 64; ++i) {
        acc = acc * 31 + static_cast<T>(i);
    }
    return acc;
}

#ifdef WIDGET_EXTERN
// Explicit instantiation declaration: "some other TU instantiates Widget<int>".
extern template struct Widget<int>;
#endif
