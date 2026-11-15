#!/bin/sh
# fhs_probe.sh - probe what a script can and cannot assume about the file hierarchy.
#
# Usage: fhs_probe.sh MODE [ARG...]
#   posix-files              the directories and devices POSIX.1-2024 XBD 10.1 requires
#   layout                   merged or split /usr, /bin/sh identity, /var/run versus /run
#   standard-path            getconf PATH through command -p, then locate sh in it
#   fallback-path SHELL...   run SHELL with an empty environment; print its fallback PATH
#   xdg                      resolve the XDG base directories from the environment
#   xdg-find NAME            first NAME under XDG_CONFIG_HOME, then each XDG_CONFIG_DIRS entry
#   pidfile FILE             read a PID file tolerantly (FHS 3.0 section 3.15.2)
#   hdb-lock PID FILE        write a lock file in HDB UUCP format (FHS 3.0 section 5.9)
#
# Portable to every POSIX shell in the matrix, including native zsh: colon lists are split
# with parameter expansion, never with IFS and an unquoted expansion, which native zsh does
# not field split.
set -u

tab=$(printf '\t')
cr=$(printf '\r')

die() {
  printf 'fhs_probe: %s\n' "$*" >&2
  exit 2
}

# kind PATH: print dir, char, or missing for a path (after following symlinks).
kind() {
  if [ -d "$1" ]; then
    echo dir
  elif [ -c "$1" ]; then
    echo char
  elif [ -e "$1" ]; then
    echo other
  else
    echo missing
  fi
}

posix_files() {
  k_root=$(kind /)
  k_dev=$(kind /dev)
  k_tmp=$(kind /tmp)
  printf 'root=%s dev=%s tmp=%s\n' "$k_root" "$k_dev" "$k_tmp"
  # /dev/null: reads return EOF at once, writes are discarded (XBD 10.1).
  n=$(wc -c </dev/null) || die 'cannot read /dev/null'
  n=${n##* } # BSD wc pads the count with blanks
  if printf 'discarded\n' >/dev/null; then w=ok; else w=fail; fi
  k_null=$(kind /dev/null)
  k_tty=$(kind /dev/tty)
  printf 'null=%s null_read_bytes=%s null_write=%s\n' "$k_null" "$n" "$w"
  printf 'tty=%s\n' "$k_tty"
}

layout() {
  # -ef compares device and inode after resolving symlinks (POSIX.1-2024 test, Austin Group Defect 375).
  if [ /bin -ef /usr/bin ]; then bin='merged'; else bin='split'; fi
  if [ ! -e /usr/bin/sh ]; then
    ush=absent
  elif [ /bin/sh -ef /usr/bin/sh ]; then
    ush=same
  else
    ush=different
  fi
  if [ ! -e /run ]; then
    vr=no-run
  elif [ /var/run -ef /run ]; then
    vr=same
  else
    vr=separate
  fi
  printf 'bin=%s usr_bin_sh=%s var_run=%s\n' "$bin" "$ush" "$vr"
}

# first_in_list NAME LIST: print the first absolute entry of a colon list holding an executable NAME.
first_in_list() {
  rest=$2:
  while [ -n "$rest" ]; do
    entry=${rest%%:*}
    rest=${rest#*:}
    case $entry in
      /*)
        if [ -f "$entry/$1" ] && [ -x "$entry/$1" ]; then
          printf '%s\n' "$entry/$1"
          return 0
        fi
        ;;
      *) ;; # empty or relative: means the current directory in PATH; never trust it here
    esac
  done
  return 1
}

standard_path() {
  # command -p makes the lookup of getconf itself independent of the caller's PATH.
  std=$(command -p getconf PATH) || die 'command -p getconf PATH failed'
  [ -n "$std" ] || die 'getconf PATH printed nothing'
  printf 'getconf_path=%s\n' "$std"
  shpath=$(first_in_list sh "$std") || die "no sh in $std"
  printf 'sh=%s\n' "$shpath"
}

fallback_path() {
  [ "$#" -ge 1 ] || die 'fallback-path needs a shell command'
  case $1 in
    /*) ;;
    *) die 'fallback-path needs an absolute shell path (env -i leaves no PATH to search)' ;;
  esac
  # shellcheck disable=SC2016 # the expansions belong to the shell started with env -i
  /usr/bin/env -i "$@" -c '
    printf "path=%s\n" "${PATH-<unset>}"
    if command -v ls >/dev/null 2>&1; then echo search=found; else echo search=notfound; fi'
}

# abs_or SETTING DEFAULT: XDG rule - an unset, empty, or relative setting is ignored.
abs_or() {
  case $1 in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "$2" ;;
  esac
}

# abs_list LIST DEFAULT: keep only the absolute entries of a colon list; DEFAULT if none remain.
abs_list() {
  rest=$1:
  out=
  while [ -n "$rest" ]; do
    entry=${rest%%:*}
    rest=${rest#*:}
    case $entry in
      /*) out=${out:+$out:}$entry ;;
      *) ;; # empty or relative entries are invalid under the XDG specification
    esac
  done
  printf '%s\n' "${out:-$2}"
}

xdg() {
  [ -n "${HOME-}" ] || die 'HOME is unset or empty'
  config_home=$(abs_or "${XDG_CONFIG_HOME-}" "$HOME/.config")
  data_home=$(abs_or "${XDG_DATA_HOME-}" "$HOME/.local/share")
  state_home=$(abs_or "${XDG_STATE_HOME-}" "$HOME/.local/state")
  cache_home=$(abs_or "${XDG_CACHE_HOME-}" "$HOME/.cache")
  config_dirs=$(abs_list "${XDG_CONFIG_DIRS-}" /etc/xdg)
  data_dirs=$(abs_list "${XDG_DATA_DIRS-}" /usr/local/share/:/usr/share/)
  printf 'config_home=%s\n' "$config_home"
  printf 'data_home=%s\n' "$data_home"
  printf 'state_home=%s\n' "$state_home"
  printf 'cache_home=%s\n' "$cache_home"
  printf 'config_dirs=%s\n' "$config_dirs"
  printf 'data_dirs=%s\n' "$data_dirs"
  rt=${XDG_RUNTIME_DIR-}
  case $rt in
    /*)
      # The specification requires the user to own it with mode 0700.
      line=$(ls -ldn -- "$rt" 2>/dev/null) || line=
      uid=$(id -u)
      perms=''
      owner=''
      read -r perms _ owner _ <<EOF
$line
EOF
      case $perms in
        drwx------*)
          if [ "$owner" = "$uid" ]; then state=ok; else state=insecure; fi
          ;;
        '') state=missing ;;
        *) state=insecure ;;
      esac
      printf 'runtime_dir=%s %s\n' "$rt" "$state"
      ;;
    *) printf 'runtime_dir=unset\n' ;;
  esac
}

xdg_find() {
  [ "$#" -eq 1 ] || die 'xdg-find needs exactly one NAME'
  [ -n "${HOME-}" ] || die 'HOME is unset or empty'
  config_home=$(abs_or "${XDG_CONFIG_HOME-}" "$HOME/.config")
  config_dirs=$(abs_list "${XDG_CONFIG_DIRS-}" /etc/xdg)
  dirs=$config_home:$config_dirs:
  while [ -n "$dirs" ]; do
    d=${dirs%%:*}
    dirs=${dirs#*:}
    if [ -f "$d/$1" ]; then
      printf '%s\n' "$d/$1"
      return 0
    fi
  done
  echo none
  return 1
}

# pidfile FILE: FHS 3.0 3.15.2 - ignore extra whitespace, leading zeroes, a missing
# trailing newline, and additional lines. Leading zeroes are stripped textually, never with
# $((...)), because POSIX arithmetic reads 010 as octal.
pidfile() {
  [ "$#" -eq 1 ] || die 'pidfile needs exactly one FILE'
  line=
  IFS= read -r line <"$1" || [ -n "$line" ] || die "empty or unreadable: $1"
  n=$line
  while :; do
    case $n in
      ' '* | "$tab"*) n=${n#?} ;;
      *) break ;;
    esac
  done
  while :; do
    case $n in
      *' ' | *"$tab" | *"$cr") n=${n%?} ;;
      *) break ;;
    esac
  done
  while :; do
    case $n in
      0?*) n=${n#0} ;;
      *) break ;;
    esac
  done
  case $n in
    '' | *[!0123456789]*) die "not a PID: $line" ;;
    *) ;;
  esac
  printf 'pid=%s\n' "$n"
}

# hdb_lock PID FILE: ten-byte right-aligned ASCII decimal PID plus newline (FHS 3.0 5.9).
hdb_lock() {
  [ "$#" -eq 2 ] || die 'hdb-lock needs PID and FILE'
  case $1 in
    '' | *[!0123456789]*) die "not a PID: $1" ;;
    *) ;;
  esac
  printf '%10s\n' "$1" >"$2" || die "cannot write $2"
}

[ "$#" -ge 1 ] || die 'usage: fhs_probe.sh MODE [ARG...]'
mode=$1
shift
case $mode in
  posix-files) posix_files ;;
  layout) layout ;;
  standard-path) standard_path ;;
  fallback-path) fallback_path "$@" ;;
  xdg) xdg ;;
  xdg-find) xdg_find "$@" ;;
  pidfile) pidfile "$@" ;;
  hdb-lock) hdb_lock "$@" ;;
  *) die "unknown mode: $mode" ;;
esac
