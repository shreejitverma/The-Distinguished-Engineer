// heavy_meta.hpp - a deliberately expensive header.
// Every translation unit that includes it pays for the recursive class
// template instantiations below while the header is being parsed, so the
// cost shows up inside this header's "Source" span in the -ftime-trace output.
#pragma once

namespace meta {

// Recursive class template: Fib<N> needs Fib<N-1> and Fib<N-2>.
// Clang memoizes specializations per translation unit, so Fib<N> costs about
// N InstantiateClass events, not 2^N, but every TU pays them again.
template <unsigned N>
struct Fib {
    static constexpr unsigned long long value = Fib<N - 1>::value + Fib<N - 2>::value;
};
template <>
struct Fib<1> {
    static constexpr unsigned long long value = 1;
};
template <>
struct Fib<0> {
    static constexpr unsigned long long value = 0;
};

// A second recursive family: a type list built one element at a time.
template <class... Ts>
struct List {};

template <unsigned N, class Acc>
struct Build;
template <class... Ts>
struct Build<0, List<Ts...>> {
    using type = List<Ts...>;
};
template <unsigned N, class... Ts>
struct Build<N, List<Ts...>> {
    using type = typename Build<N - 1, List<Ts..., char[N]>>::type;
};

template <class L>
struct Size;
template <class... Ts>
struct Size<List<Ts...>> {
    static constexpr unsigned value = sizeof...(Ts);
};

// Instantiated at namespace scope, so the work happens while this header is parsed.
inline constexpr unsigned long long kFib = Fib<900>::value;
inline constexpr unsigned kListSize = Size<Build<700, List<>>::type>::value;

}  // namespace meta
