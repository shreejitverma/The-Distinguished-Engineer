#!/usr/bin/env bash
# Build every flavor of the extension into the directory given as $1.
#
#   recscore_<variant>        plain build, used for every timed measurement
#   recscore_<variant>_probe  same source with -include probe.h (call counters)
#   allocspy                  PyMem_SetAllocator counting hooks
#
# Usage: bash build.sh <outdir>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: build.sh <outdir>}"
mkdir -p "${OUT}"

INC="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
EXT="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
if [ ! -f "${INC}/Python.h" ]; then
    echo "build.sh: ${INC}/Python.h not found; install the CPython development headers" >&2
    exit 1
fi

CFLAGS=(-O2 -g -fno-omit-frame-pointer -fPIC -Wall -Wextra -Wno-unused-parameter -Wno-unused-function)
case "$(uname -s)" in
Darwin) LINK=(-bundle -undefined dynamic_lookup) ;;
*) LINK=(-shared) ;;
esac

n=0
for variant in hold base regr gilfix fixed; do
    "${CC:-cc}" "${CFLAGS[@]}" "${LINK[@]}" -I"${INC}" \
        -DVARIANT="${n}" -DMODNAME="recscore_${variant}" \
        "${SCRIPT_DIR}/recscore.c" -o "${OUT}/recscore_${variant}${EXT}"
    "${CC:-cc}" "${CFLAGS[@]}" "${LINK[@]}" -I"${INC}" -include "${SCRIPT_DIR}/probe.h" \
        -DVARIANT="${n}" -DMODNAME="recscore_${variant}_probe" \
        "${SCRIPT_DIR}/recscore.c" -o "${OUT}/recscore_${variant}_probe${EXT}"
    n=$((n + 1))
done
"${CC:-cc}" "${CFLAGS[@]}" "${LINK[@]}" -I"${INC}" \
    "${SCRIPT_DIR}/allocspy.c" -o "${OUT}/allocspy${EXT}"
