#!/bin/sh
# probe_userland.sh: probe userland traits across GNU, BSD, BusyBox, and uutils.

set -eu

probe_flavor() {
  cp_out=$(cp --version 2>&1 || true)
  case "${cp_out}" in
    *"GNU coreutils"*)
      printf 'gnu\n'
      ;;
    *"BusyBox"*)
      printf 'busybox\n'
      ;;
    *"uutils coreutils"* | *"coreutils (uutils)"*)
      printf 'uutils\n'
      ;;
    *)
      ls_err=$(ls --version 2>&1 || true)
      case "${ls_err}" in
        *"illegal option"* | *"invalid option"* | *"unrecognized option"* | *"usage:"*)
          printf 'bsd\n'
          ;;
        *)
          printf 'unknown\n'
          ;;
      esac
      ;;
  esac
}

probe_permute() {
  dir="$1"
  src="${dir}/perm_src"
  dst="${dir}/perm_dst"
  printf 'data\n' >"${src}"
  if cp "${src}" "${dst}" -p >/dev/null 2>&1; then
    printf 'permute\n'
  else
    printf 'strict\n'
  fi
}

probe_stat_size() {
  file="$1"
  if [ ! -f "${file}" ]; then
    printf 'error: file not found\n' >&2
    return 1
  fi
  size=$(stat -c '%s' "${file}" 2>/dev/null || true)
  if [ -n "${size}" ]; then
    printf '%s\n' "${size}"
    return 0
  fi
  size=$(stat -f '%z' "${file}" 2>/dev/null || true)
  if [ -n "${size}" ]; then
    printf '%s\n' "${size}"
    return 0
  fi
  raw_wc=$(wc -c <"${file}")
  size=$(printf '%s\n' "${raw_wc}" | tr -d ' ')
  printf '%s\n' "${size}"
}

probe_date_epoch() {
  epoch="$1"
  res=$(date -u -d "@${epoch}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || true)
  if [ -n "${res}" ]; then
    printf '%s\n' "${res}"
    return 0
  fi
  res=$(date -u -r "${epoch}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || true)
  if [ -n "${res}" ]; then
    printf '%s\n' "${res}"
    return 0
  fi
  printf 'unsupported\n'
  return 1
}

probe_sed_inplace() {
  file="$1"
  pattern="$2"
  replacement="$3"
  tmp_test="${file}.probe_tmp"
  cp "${file}" "${tmp_test}"
  if sed -i '' "s/${pattern}/${replacement}/" "${tmp_test}" 2>/dev/null; then
    sed -i '' "s/${pattern}/${replacement}/" "${file}"
    rm -f "${tmp_test}"
  else
    rm -f "${tmp_test}"
    sed -i "s/${pattern}/${replacement}/" "${file}"
  fi
}

mode="${1:-help}"
case "${mode}" in
  flavor)
    probe_flavor
    ;;
  permute)
    if [ "$#" -lt 2 ]; then
      printf 'usage: %s permute <dir>\n' "$0" >&2
      exit 1
    fi
    probe_permute "$2"
    ;;
  stat_size)
    if [ "$#" -lt 2 ]; then
      printf 'usage: %s stat_size <file>\n' "$0" >&2
      exit 1
    fi
    probe_stat_size "$2"
    ;;
  date_epoch)
    if [ "$#" -lt 2 ]; then
      printf 'usage: %s date_epoch <seconds>\n' "$0" >&2
      exit 1
    fi
    probe_date_epoch "$2"
    ;;
  sed_inplace)
    if [ "$#" -lt 4 ]; then
      printf 'usage: %s sed_inplace <file> <pattern> <replacement>\n' "$0" >&2
      exit 1
    fi
    probe_sed_inplace "$2" "$3" "$4"
    ;;
  *)
    printf 'usage: %s {flavor|permute <dir>|stat_size <file>|date_epoch <seconds>|sed_inplace <file> <pat> <rep>}\n' "$0" >&2
    exit 1
    ;;
esac
