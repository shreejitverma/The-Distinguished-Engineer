#!/bin/sh
# installer-wrapped.sh: the same installer with every command inside one function.
# Nothing runs until the final line `main "$@"` has been read in full, so a truncated download
# either fails to parse (end of file inside the function body) or never calls main.
main() {
  set -eu
  remove() {
    printf 'REMOVE %s\n' "$1"
  }
  echo "install: begin"
  remove /opt/tool/cache
  echo "install: done"
}
main "$@"
