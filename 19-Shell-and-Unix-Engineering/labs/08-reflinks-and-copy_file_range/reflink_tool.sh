#!/bin/sh
# reflink_tool.sh: pick the cheapest correct file copy that the local cp offers.
# A clone (reflink) shares blocks copy-on-write; a copy writes every byte again.
set -eu

usage() {
  printf 'usage: %s <command> [args...]\n' "${0}" >&2
  printf 'commands:\n' >&2
  printf '  compile SRC_C OUT       build the C probe with CC (default cc)\n' >&2
  printf '  cp-flavor               print gnu, uutils, busybox, bsd, or unknown\n' >&2
  printf '  best-copy SRC DST       clone if the file system can, else copy\n' >&2
  exit 2
}

cmd_compile() {
  src=${1:?missing C source}
  out=${2:?missing output path}
  "${CC:-cc}" -Wall -Wextra -Werror -O2 "${src}" -o "${out}"
}

# cp_flavor: identify cp by what it prints for --version, never by the platform.
# GNU prints "cp (GNU coreutils) 9.7", uutils "cp (uutils coreutils) ...",
# BusyBox rejects the option with a usage banner naming BusyBox, and the BSD and
# macOS cp reject it with "usage: cp".
cmd_cp_flavor() {
  out=$(cp --version 2>&1) || true
  case ${out} in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: cp"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# busybox_reflink_option DIR: print --reflink=auto if BusyBox was built with FEATURE_CP_REFLINK.
# Its --help does not say so; try it on an empty file.
busybox_reflink_option() {
  probe_dir=$(mktemp -d "${1}/.reflink-probe.XXXXXX") || return
  if ! (: >"${probe_dir}/a"); then
    rmdir "${probe_dir}"
    return 1
  fi
  if cp --reflink=auto "${probe_dir}/a" "${probe_dir}/b" 2>/dev/null; then
    option=--reflink=auto
  else
    option=
  fi
  rm -f "${probe_dir}/a" "${probe_dir}/b" || return
  rmdir "${probe_dir}" || return
  printf '%s' "${option}"
}

# best_copy SRC DST: clone when possible, fall back to a byte copy, and say which cp ran.
cmd_best_copy() {
  src=${1:?missing source}
  dst=${2:?missing destination}
  flavor=$(cmd_cp_flavor)
  case ${flavor} in
    gnu | uutils)
      # Default since coreutils 9.0, spelled out so older releases clone too.
      set -- cp --reflink=auto
      ;;
    busybox)
      dst_dir=$(dirname -- "${dst}")
      option=$(busybox_reflink_option "${dst_dir}")
      set -- cp ${option:+"${option}"}
      ;;
    bsd)
      # macOS cp -c tries clonefile(2) and falls back to a copy on EXDEV or ENOTSUP;
      # FreeBSD 13+ cp already calls copy_file_range(2), which may clone on its own.
      sysname=$(uname -s)
      if [ "${sysname}" = Darwin ]; then
        set -- cp -c
      else
        set -- cp
      fi
      ;;
    *)
      set -- cp
      ;;
  esac
  printf 'via: %s\n' "$*"
  "$@" -- "${src}" "${dst}"
}

[ "$#" -ge 1 ] || usage
command=$1
shift
case ${command} in
  compile) cmd_compile "$@" ;;
  cp-flavor) cmd_cp_flavor ;;
  best-copy) cmd_best_copy "$@" ;;
  *) usage ;;
esac
