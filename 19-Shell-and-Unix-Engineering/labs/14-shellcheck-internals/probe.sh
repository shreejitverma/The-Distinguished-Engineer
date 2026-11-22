#!/bin/sh
# probe.sh MODE: print what the shell running this script does for a construct ShellCheck models.
#   ifs-split  v=a:b with IFS=: then an unquoted $v; print how many fields the shell produced.
#   lastpipe   a pipeline whose last stage is `read`; print what the parent shell sees in v afterwards.
# ShellCheck decides both from its own dialect model, never from the shell that will run the script.

# shellcheck disable=SC2030,SC2031 # lastpipe mode reads a variable set in a pipeline stage on purpose
case ${1-} in
  ifs-split)
    v=a:b
    IFS=:
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the splitting being probed
    set -- $v
    printf '%s\n' "$#"
    ;;
  lastpipe)
    echo hi | read -r v
    printf '%s\n' "${v-unset}"
    ;;
  *)
    echo "usage: probe.sh ifs-split|lastpipe" >&2
    exit 2
    ;;
esac
