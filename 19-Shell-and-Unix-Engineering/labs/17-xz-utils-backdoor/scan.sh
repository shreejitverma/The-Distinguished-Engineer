#!/bin/sh
# Flag build glue that decodes hidden data and feeds it to a shell, the shape of the xz-utils hook.
# usage: scan.sh DIR
# Prints PATH:LINE:RULE per hit and exits 1 when there is any, 0 when clean, 2 on bad usage.
# Rules: DECOMPRESS-TO-SHELL, EVAL-TO-SHELL, SKIP-CHAIN, MARKER-DISCOVERY.
LC_ALL=C
export LC_ALL
if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
  echo "usage: scan.sh DIR" >&2
  exit 2
fi
# shellcheck disable=SC2016 # the pattern keeps literal dollar signs for grep
shell='(/bin/(ba)?sh|(ba)?sh|\$SHELL|\$\(SHELL\)|\$\{SHELL\})([[:space:]]|$)'
r_decompress="(xz|unxz|lzma|gzip|gunzip|bzip2|zstd)[^|]*\\|[[:space:]]*$shell"
r_eval="eval[[:space:]][^|]*\\|[[:space:]]*$shell"
r_skip='head[[:space:]]+-c[[:space:]]*\+?[0-9]+[[:space:]]*>[[:space:]]*/dev/null'
r_marker='grep[[:space:]].*#\{4\}'

scan_tree() {
  find "$1" -type f | sort | while IFS= read -r f; do
    case $f in
      *.m4 | */configure | */configure.ac | */Makefile.am | */Makefile.in | *.mk | *.sh) ;;
      *) continue ;;
    esac
    for rule in DECOMPRESS-TO-SHELL:"$r_decompress" EVAL-TO-SHELL:"$r_eval" SKIP-CHAIN:"$r_skip" MARKER-DISCOVERY:"$r_marker"; do
      grep -n -E "${rule#*:}" "$f" | awk -F: -v f="$f" -v r="${rule%%:*}" '{ print f ":" $1 ":" r }'
    done
  done
}

hits=$(scan_tree "$1")
if [ -n "$hits" ]; then
  printf '%s\n' "$hits"
  exit 1
fi
exit 0
