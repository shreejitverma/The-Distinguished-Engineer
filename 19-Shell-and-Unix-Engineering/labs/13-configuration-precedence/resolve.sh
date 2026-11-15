#!/bin/sh
# resolve.sh: resolve the settings of a hypothetical tool, demo, from every layer and print
# each final value with the layer that set it. Precedence, lowest first:
#   1. built-in defaults
#   2. system files: demo/config under each $XDG_CONFIG_DIRS entry (default /etc/xdg),
#      read last entry first, so the first entry listed wins
#   3. NO_COLOR, a cross-tool preference, so a tool's own user file overrides it
#   4. user file: $XDG_CONFIG_HOME/demo/config (default $HOME/.config/demo/config)
#   5. DEMO_* environment variables that are set and not empty
#   6. command-line options, where the last occurrence of an option wins
# --config FILE (or a non-empty DEMO_CONFIG) replaces layers 2 and 4 with FILE, which must
# exist; --no-config skips every file. Files are data and are never sourced: KEY = VALUE
# lines, whole-line # comments, known keys only, last duplicate wins.
# Usage: resolve.sh [--config FILE | --no-config] [--print-files] [--port N]
#                   [--log-level LEVEL] [--color MODE] [--greeting TEXT]
# Exit status: 0 resolved, 2 usage or configuration error.

tab=$(printf '\t')
cr=$(printf '\r')

die() {
  printf 'resolve.sh: %s\n' "$*" >&2
  exit 2
}

# trim TEXT: set trimmed to TEXT without leading and trailing spaces and tabs.
trim() {
  trimmed=$1
  while :; do
    case $trimmed in
      " "* | "$tab"*) trimmed=${trimmed#?} ;;
      *" " | *"$tab") trimmed=${trimmed%?} ;;
      *) return 0 ;;
    esac
  done
}

# set_value KEY VALUE ORIGIN: validate VALUE for KEY and record it with its origin.
# Every layer is validated when it is applied, so a bad lower layer fails even if overridden.
set_value() {
  case $1 in
    port)
      case $2 in
        '' | *[!0-9]* | 0*) die "port '$2' from $3 is not an integer from 1 to 65535" ;;
        *) ;;
      esac
      if [ "${#2}" -gt 5 ] || [ "$2" -gt 65535 ]; then
        die "port '$2' from $3 is not an integer from 1 to 65535"
      fi
      port=$2 port_from=$3
      ;;
    log_level)
      case $2 in
        debug | info | warn | error) ;;
        *) die "log_level '$2' from $3 is not one of debug, info, warn, error" ;;
      esac
      log_level=$2 log_level_from=$3
      ;;
    color)
      case $2 in
        auto | always | never) ;;
        *) die "color '$2' from $3 is not one of auto, always, never" ;;
      esac
      color=$2 color_from=$3
      ;;
    greeting) greeting=$2 greeting_from=$3 ;;
    *) die "$3: unknown key '$1'" ;;
  esac
}

# read_file FILE: apply every KEY = VALUE line of FILE, in order, as data.
read_file() {
  lineno=0
  # The || clause keeps a last line that has no trailing newline (read returns 1 at EOF).
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line=${line%"$cr"}
    trim "$line"
    line=$trimmed
    case $line in
      '' | '#'*) continue ;;
      *=*) ;;
      *) die "$1:$lineno: expected KEY = VALUE" ;;
    esac
    trim "${line%%=*}"
    key=$trimmed
    trim "${line#*=}"
    set_value "$key" "$trimmed" "$1:$lineno"
  done <"$1"
}

# need_value OPTION ARG...: fail unless OPTION is followed by a value.
need_value() {
  [ "$#" -ge 2 ] || die "option $1 needs a value"
}

# Pass 1: parse the command line into cli_* variables without applying anything yet,
# because --config decides which files are read before any value is merged.
no_config='' print_files='' cli_config_set=''
cli_port_set='' cli_log_level_set='' cli_color_set='' cli_greeting_set=''
while [ "$#" -gt 0 ]; do
  case $1 in
    --config)
      need_value "$@"
      cli_config=$2 cli_config_set=1
      shift 2
      ;;
    --no-config)
      no_config=1
      shift
      ;;
    --print-files)
      print_files=1
      shift
      ;;
    --port)
      need_value "$@"
      cli_port=$2 cli_port_set=1
      shift 2
      ;;
    --log-level)
      need_value "$@"
      cli_log_level=$2 cli_log_level_set=1
      shift 2
      ;;
    --color)
      need_value "$@"
      cli_color=$2 cli_color_set=1
      shift 2
      ;;
    --greeting)
      need_value "$@"
      cli_greeting=$2 cli_greeting_set=1
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *) die "unknown option '$1'" ;;
  esac
done
[ "$#" -eq 0 ] || die "unexpected operand '$1'"
if [ -n "$no_config" ] && [ -n "$cli_config_set" ]; then
  die "--config and --no-config are mutually exclusive"
fi

# Choose the files. An explicit file must exist; implicit files are skipped when unreadable,
# as the XDG Base Directory Specification asks. Relative XDG paths are invalid and ignored.
explicit=''
if [ -n "$no_config" ]; then
  :
elif [ -n "$cli_config_set" ]; then
  explicit=$cli_config
elif [ -n "${DEMO_CONFIG:-}" ]; then
  explicit=$DEMO_CONFIG
fi
if [ -n "$explicit" ] && { [ ! -f "$explicit" ] || [ ! -r "$explicit" ]; }; then
  die "config file '$explicit' is not a readable file"
fi

user_file=''
case ${XDG_CONFIG_HOME:-} in
  /*) user_file=$XDG_CONFIG_HOME/demo/config ;;
  *)
    case ${HOME:-} in
      /*) user_file=$HOME/.config/demo/config ;;
      *) ;;
    esac
    ;;
esac

# Collect the system files into the positional parameters in reverse order (first entry last),
# so reading them in order lets the most important directory write last and win.
set --
rest=${XDG_CONFIG_DIRS:-/etc/xdg}
while :; do
  dir=${rest%%:*}
  case $dir in
    /*) set -- "$dir/demo/config" "$@" ;;
    *) ;;
  esac
  case $rest in
    *:*) rest=${rest#*:} ;;
    *) break ;;
  esac
done

if [ -n "$print_files" ]; then
  if [ -n "$no_config" ]; then
    printf 'none\n'
  elif [ -n "$explicit" ]; then
    printf 'explicit %s\n' "$explicit"
  else
    for f in "$@"; do
      printf 'system %s\n' "$f"
    done
    if [ -n "$user_file" ]; then
      printf 'user %s\n' "$user_file"
    fi
  fi
  exit 0
fi

# Pass 2: merge, lowest precedence first; each later set_value overwrites an earlier one.
set_value port 8080 default
set_value log_level info default
set_value color auto default
set_value greeting hello default

if [ -z "$no_config" ] && [ -z "$explicit" ]; then
  for f in "$@"; do
    if [ -f "$f" ] && [ -r "$f" ]; then
      read_file "$f"
    fi
  done
fi
if [ -n "${NO_COLOR:-}" ]; then
  set_value color never "env NO_COLOR"
fi
if [ -n "$explicit" ]; then
  read_file "$explicit"
elif [ -z "$no_config" ] && [ -n "$user_file" ] && [ -f "$user_file" ] && [ -r "$user_file" ]; then
  read_file "$user_file"
fi

if [ -n "${DEMO_PORT:-}" ]; then set_value port "$DEMO_PORT" "env DEMO_PORT"; fi
if [ -n "${DEMO_LOG_LEVEL:-}" ]; then set_value log_level "$DEMO_LOG_LEVEL" "env DEMO_LOG_LEVEL"; fi
if [ -n "${DEMO_COLOR:-}" ]; then set_value color "$DEMO_COLOR" "env DEMO_COLOR"; fi
if [ -n "${DEMO_GREETING:-}" ]; then set_value greeting "$DEMO_GREETING" "env DEMO_GREETING"; fi

if [ -n "$cli_port_set" ]; then set_value port "$cli_port" "option --port"; fi
if [ -n "$cli_log_level_set" ]; then set_value log_level "$cli_log_level" "option --log-level"; fi
if [ -n "$cli_color_set" ]; then set_value color "$cli_color" "option --color"; fi
if [ -n "$cli_greeting_set" ]; then set_value greeting "$cli_greeting" "option --greeting"; fi

printf 'port=%s (%s)\n' "$port" "$port_from"
printf 'log_level=%s (%s)\n' "$log_level" "$log_level_from"
printf 'color=%s (%s)\n' "$color" "$color_from"
printf 'greeting=%s (%s)\n' "$greeting" "$greeting_from"
