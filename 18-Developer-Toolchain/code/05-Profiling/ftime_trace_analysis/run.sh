#!/usr/bin/env bash
# Lab: read clang -ftime-trace output the way ClangBuildAnalyzer does.
# Needs a clang (Apple clang or LLVM clang 19 or later) and python3; GCC has no -ftime-trace.
# No harness requirements: it runs on macOS as is, and on Linux wherever clang++ is installed.
set -euo pipefail

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W="$(mktemp -d)"
trap 'rm -rf "${W}"' EXIT
export LC_ALL=C

# Use ${CXX:-c++} when it understands -ftime-trace (Apple clang, LLVM clang); otherwise
# fall back to clang++, because GCC rejects the flag ("unrecognized command-line option").
CXX="${CXX:-c++}"
probe() { echo 'int x;' | "$1" -x c++ -fsyntax-only -ftime-trace=/dev/null - >/dev/null 2>&1; }
if ! probe "${CXX}"; then
    if command -v clang++ >/dev/null && probe clang++; then
        CXX=clang++
    else
        echo "error: ${CXX} does not accept -ftime-trace and no clang++ was found" >&2
        exit 1
    fi
fi

SRC="${LAB}/src"
cp -R "${SRC}" "${W}/src"
python3 "${LAB}/gen_backend.py" "${W}/src/backend_heavy.cpp"
cd "${W}"
STD=(-std=c++17)

echo "== 1. Where the trace file goes (clang driver rules)"
mkdir -p naming/obj naming/tracedir naming/link
"${CXX}" "${STD[@]}" -c src/tu_a.cpp -o naming/obj/tu_a.o -ftime-trace
"${CXX}" "${STD[@]}" -c src/tu_a.cpp -o naming/obj/other.o -ftime-trace=naming/tracedir
"${CXX}" "${STD[@]}" -c src/tu_a.cpp -o naming/obj/third.o -ftime-trace=naming/custom-name.json
"${CXX}" "${STD[@]}" src/main.cpp src/tu_a.cpp -o naming/link/app -ftime-trace
printf '%-58s -> %s\n' "-c tu_a.cpp -o obj/tu_a.o -ftime-trace" "$(cd naming && ls obj/*.json)"
printf '%-58s -> %s\n' "-c tu_a.cpp -o obj/other.o -ftime-trace=tracedir" "$(cd naming && ls tracedir)"
printf '%-58s -> %s\n' "-c tu_a.cpp -o obj/third.o -ftime-trace=custom-name.json" "$(cd naming && ls *.json)"
printf '%-58s -> %s\n' "main.cpp tu_a.cpp -o link/app -ftime-trace" "$(cd naming/link && ls *.json | tr '\n' ' ' | sed 's/ $//')"
echo

# compile <outdir> <source-stem> [extra flags...]: one -c compile with a trace next to the object.
compile() {
    local out="$1" stem="$2"
    shift 2
    mkdir -p "${out}"
    "${CXX}" "${STD[@]}" -c "src/${stem}.cpp" -o "${out}/${stem}.o" -ftime-trace "$@"
}

# Granularity: the same TU with every event kept, and with the default 500 us filter.
compile g0 tu_a -O2 -ftime-trace-granularity=0
compile g500 tu_a -O2

# A five-TU "build" analyzed as a whole.
for s in backend_heavy main tu_a tu_b tu_c; do
    compile base "${s}" -O2 -ftime-trace-granularity=0
done

# extern template: implicit instantiation in every TU, versus one explicit instantiation.
for s in tu_a tu_b tu_c; do
    compile imp_O0 "${s}" -O0 -ftime-trace-granularity=0
    compile imp_O2 "${s}" -O2 -ftime-trace-granularity=0
    compile ext_O0 "${s}" -O0 -ftime-trace-granularity=0 -DWIDGET_EXTERN
    compile ext_O2 "${s}" -O2 -ftime-trace-granularity=0 -DWIDGET_EXTERN
done
compile ext_O2 widget_inst -O2 -ftime-trace-granularity=0 -DWIDGET_EXTERN

# Precompiled header: build heavy_meta.hpp once, then reuse it in tu_a.
mkdir -p pch
"${CXX}" "${STD[@]}" -O2 -x c++-header src/heavy_meta.hpp -o pch/heavy_meta.pch \
    -ftime-trace=pch/heavy_meta.json -ftime-trace-granularity=0
compile pch tu_a -O2 -ftime-trace-granularity=0 -include-pch pch/heavy_meta.pch

# Verbose mode adds source locations to instantiation events.
compile verbose tu_a -O2 -ftime-trace-granularity=0 -ftime-trace-verbose

python3 "${LAB}/analyze.py" "${W}"
