#!/usr/bin/env bash
# Make and Ninja mechanics lab: dyndep ordering and restat, the binary deps log,
# the command-hash build log, header deletion under Make and Ninja, grouped targets,
# and Ninja joining a GNU Make jobserver as a client.
# Needs: ninja >= 1.13, GNU Make >= 4.4 (as gmake or make), python3, and a C compiler.
# requires: macos
# Recorded on macOS with Homebrew ninja and gmake; no Linux expected file exists yet.
set -euo pipefail

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C
export NINJA_STATUS="[%f/%t] "
unset MAKEFLAGS MFLAGS MAKELEVEL || true
CC="${CC:-cc}"

die() { echo "lab error: $*" >&2; exit 1; }

command -v ninja >/dev/null || die "ninja not found"
command -v python3 >/dev/null || die "python3 not found"
ninja_ver="$(ninja --version)"
case "${ninja_ver}" in 1.1[3-9]*|1.[2-9][0-9]*) ;; *) die "need ninja >= 1.13, found ${ninja_ver}" ;; esac

GMAKE=""
for m in gmake make; do
    if command -v "$m" >/dev/null && "$m" --version 2>/dev/null | head -1 | grep -Eq 'GNU Make (4\.([4-9]|[1-9][0-9])|[5-9])'; then
        GMAKE="$m"; break
    fi
done
[ -n "${GMAKE}" ] || die "need GNU Make >= 4.4 as gmake or make"
# Normalize the program name in Make's diagnostics so the output does not depend on it.
norm_make() { sed -E 's/^g?make(\[[0-9]+\])?:/make:/'; }
# Apple clang lists the SDK's SDKSettings.json in every depfile; its path depends on the Xcode install.
scrub_sdk() { sed -E 's#/[^ :]*/SDKSettings\.json#<SDK>/SDKSettings.json#g'; }

echo "== Versions"
echo "ninja ${ninja_ver}"
"${GMAKE}" --version | head -1
echo "jobserver-fifo in .FEATURES: $(printf 'all:;@echo $(filter jobserver-fifo,$(.FEATURES))\n' | "${GMAKE}" -s -f -)"
echo

###############################################################################
echo "== 1. dyndep: discovering module dependencies during the build"
D1="${WORK}/dyndep"; mkdir -p "${D1}"; cd "${D1}"
cat >a.src <<'EOF'
module m
export answer
body version 1
EOF
cat >b.src <<'EOF'
use m
body client
EOF
write_ninja() {  # $1 = "yes" to bind dyndep, anything else to omit it
    {
        echo "lab = ${LAB}"
        cat <<'EOF'
rule scan
  command = python3 $lab/modscan.py $out $in
  description = SCAN $out
rule mc
  command = python3 $lab/modc.py $in $out
  description = MC $out
rule link
  command = cat $in > $out
  description = LINK $out
build mods.dd: scan b.src a.src
EOF
        # b.o is declared before a.o, so with equal priority b.o has the lower edge id and goes first.
        echo "build b.o: mc b.src || mods.dd"
        [ "$1" = yes ] && echo "  dyndep = mods.dd"
        echo "build a.o: mc a.src || mods.dd"
        [ "$1" = yes ] && echo "  dyndep = mods.dd"
        echo "build app: link a.o b.o"
    } >build.ninja
}
write_ninja yes
ninja -j8 >/dev/null
echo "-- clean build at -j8; execution order from compile.log:"
sed 's/^/  /' compile.log
echo "-- generated dyndep file mods.dd:"
sed 's/^/  /' mods.dd
echo "-- second run:"
ninja -j8 | sed 's/^/  /'

: >compile.log
sed -i.bak 's/body version 1/body version 2/' a.src && rm -f a.src.bak
ninja -j8 >/dev/null
echo "-- edit a.src outside its exports (m.mod content unchanged, restat = 1):"
sed 's/^/  /' compile.log
echo "  b.o rebuilt: $(grep -q 'compile b.src' compile.log && echo yes || echo no)"

: >compile.log
sed -i.bak 's/export answer/export answer2/' a.src && rm -f a.src.bak
ninja -j8 >/dev/null
echo "-- edit a.src exports (m.mod content changes):"
sed 's/^/  /' compile.log
echo "  b.o rebuilt: $(grep -q 'compile b.src' compile.log && echo yes || echo no)"

echo "-- same graph without the dyndep bindings, clean build at -j1:"
rm -rf "${D1:?}"/* "${D1}"/.ninja_*
cat >a.src <<'EOF'
module m
export answer
EOF
cat >b.src <<'EOF'
use m
EOF
write_ninja no
set +e
ninja -j1 >out.txt 2>&1
rc=$?
set -e
grep -E '^(FAILED|modc:|ninja: build stopped)' out.txt | sed 's/^/  /'
echo "  ninja exit status: ${rc}"
echo

###############################################################################
echo "== 2. deps = gcc: the binary .ninja_deps log and the command-hash .ninja_log"
D2="${WORK}/deps"; mkdir -p "${D2}"; cd "${D2}"
printf '#define VALUE 41\n' >config.h
printf 'static inline int bump(int x) { return x + 1; }\n' >util.h
cat >main.c <<'EOF'
#include "config.h"
#include "util.h"
int main(void) { return bump(VALUE) == 42 ? 0 : 1; }
EOF
write_cc_ninja() {
    cat >build.ninja <<EOF
cc = ${CC}
cflags = $1
rule cc
  command = \$cc \$cflags -MD -MF \$out.d -c \$in -o \$out
  depfile = \$out.d
  deps = gcc
  description = CC \$out
build main.o: cc main.c
EOF
}
write_cc_ninja "-O0"
ninja | sed 's/^/  /'
echo "  depfile main.o.d left on disk: $([ -e main.o.d ] && echo yes || echo no)"
echo "-- .ninja_deps decoded by depslog.py:"
python3 "${LAB}/depslog.py" .ninja_deps | scrub_sdk
echo "-- ninja -t deps (mtime scrubbed):"
ninja -t deps | sed -E 's/deps mtime [0-9]+/deps mtime N/; s/^/  /' | sed '/^ *$/d' | scrub_sdk
echo "-- .ninja_log header and fields per entry:"
echo "  $(head -1 .ninja_log)"
echo "  tab-separated fields per entry: $(awk -F'\t' 'NR>1{print NF}' .ninja_log | sort -u | tr '\n' ' ')"
echo "-- change cflags only, then ninja -d explain:"
write_cc_ninja "-O1"
ninja -d explain 2>&1 >/dev/null | grep -E 'command line changed' | sed 's/^/  /'
echo "-- second run:"
ninja | sed 's/^/  /'
echo "-- delete util.h and stop including it:"
rm util.h
cat >main.c <<'EOF'
#include "config.h"
int main(void) { return VALUE == 41 ? 0 : 1; }
EOF
set +e
ninja >out.txt 2>&1
rc=$?
set -e
sed 's/^/  /' out.txt
echo "  ninja exit status: ${rc}"
echo "-- deps after the rebuild:"
python3 "${LAB}/depslog.py" .ninja_deps | tail -2 | scrub_sdk
echo

###############################################################################
echo "== 3. GNU Make: the same header deletion with -MMD, then with -MMD -MP"
for flags in "-MMD" "-MMD -MP"; do
    D3="${WORK}/make$(echo "$flags" | tr -d ' -')"; mkdir -p "${D3}"; cd "${D3}"
    printf '#define VALUE 41\n' >config.h
    printf 'static inline int bump(int x) { return x + 1; }\n' >util.h
    printf '#include "config.h"\n#include "util.h"\nint main(void) { return bump(VALUE) == 42 ? 0 : 1; }\n' >main.c
    printf 'main.o: main.c\n\t%s %s -c main.c -o main.o\n-include main.d\n' "${CC}" "${flags}" >Makefile
    "${GMAKE}" -s
    echo "-- DEPFLAGS = ${flags}; main.d:"
    tr -s ' \\\n' ' ' <main.d | scrub_sdk | sed -E 's/ +$//; s/^/  /'; echo
    rm util.h
    printf '#include "config.h"\nint main(void) { return VALUE == 41 ? 0 : 1; }\n' >main.c
    set +e
    "${GMAKE}" -s >out.txt 2>&1
    rc=$?
    set -e
    norm_make <out.txt | sed 's/^/  /'
    echo "  make exit status after deleting util.h: ${rc}"
done
echo

###############################################################################
echo "== 4. GNU Make: one recipe with two outputs, ':' versus '&:'"
for sep in ":" "&:"; do
    D4="${WORK}/group$(echo "$sep" | tr -d ':&')x"; mkdir -p "${D4}"; cd "${D4}"
    printf 'gen.h gen.c %s\n\t@echo run >>runs.log; sleep 0.2; touch gen.h gen.c\n' "${sep}" >Makefile
    for j in 1 4; do
        rm -f gen.h gen.c runs.log
        "${GMAKE}" -s -j"$j" gen.h gen.c
        echo "  rule 'gen.h gen.c ${sep}' at -j${j}: recipe ran $(wc -l <runs.log | tr -d ' ') time(s)"
    done
done
echo

###############################################################################
echo "== 5. Ninja as a GNU Make jobserver client"
D5="${WORK}/jobserver"; mkdir -p "${D5}"; cd "${D5}"
{
    echo "lab = ${LAB}"
    cat <<'EOF'
rule probe
  command = sh $lab/probe.sh $out running samples
  description = PROBE $out
EOF
    for i in 1 2 3 4 5 6 7 8 9; do echo "build p$i: probe"; done
} >build.ninja
# 'all' is an ordinary recipe; 'rec' is marked recursive with '+', which matters only for pipe jobservers.
cat >Makefile <<'EOF'
all:
	printf '%s\n' "$$MAKEFLAGS" >makeflags.txt; ninja $(NINJAFLAGS) >ninja.out 2>&1
rec:
	+printf '%s\n' "$$MAKEFLAGS" >makeflags.txt; ninja $(NINJAFLAGS) >ninja.out 2>&1
EOF
scrub_js() { sed -E 's#fifo:[^] ]+#fifo:PATH#g; s#auth=[0-9]+,[0-9]+#auth=R,W#g'; }
run_case() {  # $1 = label, rest = make arguments
    local label=$1; shift
    rm -rf running samples p? .ninja_log ninja.out makeflags.txt; mkdir running
    "${GMAKE}" -s "$@"
    echo "-- ${label}"
    echo "  MAKEFLAGS jobserver words: $(tr ' ' '\n' <makeflags.txt | grep -- '--jobserver-auth' | tr '\n' ' ' | scrub_js)"
    grep -E '^ninja: (Jobserver|warning)' ninja.out | scrub_js | sed 's/^/  /' || true
    local max; max="$(sort -n samples | tail -1)"
    if [ "${EXACT:-1}" = 1 ]; then
        echo "  edges: $(wc -l <samples | tr -d ' '), max concurrent: ${max}"
    else
        echo "  edges: $(wc -l <samples | tr -d ' '), more than 3 at once: $([ "${max}" -gt 3 ] && echo yes || echo no) (Ninja's own default is nproc + 2)"
    fi
}
run_case "make -j3, plain recipe, ninja with no -j (joins the fifo jobserver)" -j3 all
run_case "make -j3, plain recipe, ninja -j8 (an explicit -j disables the client)" -j3 all NINJAFLAGS=-j8
EXACT=0 run_case "make -j3 --jobserver-style=pipe, plain recipe (make hides the pipe)" -j3 --jobserver-style=pipe all
EXACT=0 run_case "make -j3 --jobserver-style=pipe, '+' recipe (ninja sees the pipe and declines it)" -j3 --jobserver-style=pipe rec
