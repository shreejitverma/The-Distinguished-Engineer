#!/bin/sh
# pack.sh - a portable command-line parser for short and long options in POSIX sh.
# No eval, no arrays, no external getopt: operands are rotated to the end of "$@" as they are
# found, so options may follow operands (GNU order) unless POSIXLY_CORRECT is set (POSIX order).
# The tool does no packing; it prints what it parsed, one key per line, so tests can assert it.
# Usage: pack.sh [OPTION]... [--] [FILE]...

prog=pack
version=1.0.0

usage() {
  cat <<EOF
Usage: $prog [OPTION]... [--] [FILE]...
Pack FILEs into an archive (a demonstration: it prints what it parsed).
With no FILE, or when FILE is -, read standard input.

  -o, --output=FILE      write the archive to FILE (default: standard output)
  -j, --jobs=N           run N jobs in parallel, 1 to 64 (default: 1)
  -x, --exclude=PATTERN  skip files matching PATTERN; repeatable, applied in order
  -n, --dry-run          print what would be done, change nothing
      --color[=WHEN]     color the output: always, never, or auto (default: auto)
  -v, --verbose          explain what is being done; repeat for more detail
  -h, --help             display this help and exit
  -V, --version          output version information and exit

Exit status: 0 on success, 2 on a usage error.
EOF
}

# die_usage MESSAGE: report a usage error the GNU way (program: message, then a hint) and exit 2.
die_usage() {
  printf '%s: %s\n' "$prog" "$1" >&2
  printf "Try '%s --help' for more information.\n" "$prog" >&2
  exit 2
}

# parse_jobs VALUE: accept a decimal integer from 1 to 64; reject anything else before arithmetic.
# Leading zeros are stripped textually, because $((08)) is an octal error in bash and dash.
parse_jobs() {
  case $1 in
    '' | *[!0-9]*) die_usage "invalid number of jobs: '$1'" ;;
    *) ;;
  esac
  digits=$1
  while :; do
    case $digits in
      0?*) digits=${digits#0} ;;
      *) break ;;
    esac
  done
  # Compare the length first, so a 30-digit value never reaches test(1) or $((...)).
  if [ "${#digits}" -gt 2 ] || [ "$digits" -lt 1 ] || [ "$digits" -gt 64 ]; then
    die_usage "number of jobs out of range (1 to 64): '$1'"
  fi
  jobs=$digits
}

# set_value NAME VALUE: store the option-argument VALUE of the long option --NAME.
set_value() {
  case $1 in
    output)
      if [ -z "$2" ]; then
        die_usage "option '--output' requires a non-empty argument"
      fi
      output=$2
      ;;
    jobs) parse_jobs "$2" ;;
    exclude)
      excludes="${excludes}exclude=<$2>
"
      ;;
    color)
      case $2 in
        always | never | auto) color=$2 ;;
        *) die_usage "invalid argument '$2' for '--color' (valid: always, never, auto)" ;;
      esac
      ;;
    *)
      printf '%s: internal error: no option --%s\n' "$prog" "$1" >&2
      exit 70
      ;;
  esac
}

verbose=0
dry_run=0
jobs=1
output=-
color=auto
excludes=
operands_only=0
collected=0

# posix_order: empty until needed, then 1 if POSIXLY_CORRECT is in the environment, else 0.
posix_order=

# The loop examines each original argument at most once. Unexamined arguments sit at the front of
# "$@"; every operand is appended at the back, so when the loop ends "$@" holds only operands.
# Each append copies "$@", so n permuted operands cost O(n^2); "--" before the operands avoids it.
remaining=$#
while [ "$remaining" -gt 0 ]; do
  arg=$1
  shift
  remaining=$((remaining - 1))
  if [ "$operands_only" -eq 1 ]; then
    set -- "$@" "$arg"
    continue
  fi
  case $arg in
    --)
      if [ "$collected" -eq 0 ]; then
        break # nothing rotated yet: "$@" already holds exactly the operands, in order
      fi
      operands_only=1
      ;;
    --help)
      usage
      exit 0
      ;;
    --version)
      printf '%s %s\n' "$prog" "$version"
      exit 0
      ;;
    --verbose) verbose=$((verbose + 1)) ;;
    --dry-run) dry_run=1 ;;
    --color) color=always ;;
    --color=*) set_value color "${arg#*=}" ;;
    --output=* | --jobs=* | --exclude=*)
      name=${arg%%=*}
      set_value "${name#--}" "${arg#*=}"
      ;;
    --output | --jobs | --exclude)
      if [ "$remaining" -eq 0 ]; then
        die_usage "option '$arg' requires an argument"
      fi
      value=$1
      shift
      remaining=$((remaining - 1))
      set_value "${arg#--}" "$value"
      ;;
    --help=* | --version=* | --verbose=* | --dry-run=*)
      die_usage "option '${arg%%=*}' doesn't allow an argument"
      ;;
    --*) die_usage "unrecognized option '$arg'" ;;
    -?*)
      # A cluster such as -vno FILE or -vnoFILE: flags first, at most one option-argument last.
      cluster=${arg#-}
      while [ -n "$cluster" ]; do
        opt=${cluster%"${cluster#?}"}
        cluster=${cluster#?}
        case $opt in
          v) verbose=$((verbose + 1)) ;;
          n) dry_run=1 ;;
          h)
            usage
            exit 0
            ;;
          V)
            printf '%s %s\n' "$prog" "$version"
            exit 0
            ;;
          o | j | x)
            if [ -n "$cluster" ]; then
              value=$cluster
              cluster=
            elif [ "$remaining" -gt 0 ]; then
              value=$1
              shift
              remaining=$((remaining - 1))
            else
              die_usage "option requires an argument -- '$opt'"
            fi
            case $opt in
              o) set_value output "$value" ;;
              j) set_value jobs "$value" ;;
              *) set_value exclude "$value" ;;
            esac
            ;;
          *) die_usage "invalid option -- '$opt'" ;;
        esac
      done
      ;;
    *)
      # An operand, including a lone "-" (standard input).
      if [ "$remaining" -gt 0 ] && [ -z "$posix_order" ]; then
        # Test the environment, as getopt(3) does with getenv(), not the shell variable: bash in
        # POSIX mode (macOS /bin/sh) defines an unexported POSIXLY_CORRECT=y by itself, which would
        # make one script parse differently under sh. Lazy, because env | grep costs two forks.
        posix_order=0
        if env | grep '^POSIXLY_CORRECT=' >/dev/null; then
          posix_order=1
        fi
      fi
      if [ "$posix_order" = 1 ]; then
        # POSIX order: the first operand ends the options. Nothing has been rotated yet, so put
        # it back in front and stop; "$@" is now exactly the operands, in order.
        set -- "$arg" "$@"
        break
      fi
      set -- "$@" "$arg"
      collected=$((collected + 1))
      ;;
  esac
done

printf 'verbose=%s\n' "$verbose"
printf 'dry_run=%s\n' "$dry_run"
printf 'jobs=%s\n' "$jobs"
printf 'output=%s\n' "$output"
printf 'color=%s\n' "$color"
printf '%s' "$excludes"
printf 'operands=%s\n' "$#"
for operand in "$@"; do
  printf 'operand=<%s>\n' "$operand"
done
