#!/bin/sh
# env_filter.sh: demonstrates sudo's env_reset and initial_badenv_table logic.
# Filters input environment assignments according to sudo's security rules.
#
# Usage:
#   env_filter.sh KEY=VALUE [KEY=VALUE...]

set -eu

SECURE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Check if a variable name matches sudo's initial_badenv_table
# Sets bad_env=1 when the variable is on the list, else 0.
is_bad_env() {
  var_name="$1"
  var_val="$2"
  bad_env=0

  # Reject bash function exports: *=()*
  case "$var_val" in
    \(\)\ *) bad_env=1 ;;
    *) ;;
  esac

  case "$var_name" in
    IFS | CDPATH | LOCALDOMAIN | RES_OPTIONS | HOSTALIASES | NLSPATH | PATH_LOCALE)
      bad_env=1
      ;;
    LD_* | _RLD* | SHLIB_PATH | LDR_* | LIBPATH | AUTHSTATE | DYLD_*)
      bad_env=1
      ;;
    KRB5_CONFIG* | KRB5_KTNAME | VAR_ACE | USR_ACE | DLC_ACE)
      bad_env=1
      ;;
    TERMINFO | TERMINFO_DIRS | TERMPATH | TERMCAP)
      bad_env=1
      ;;
    ENV | BASH_ENV | PS4 | GLOBIGNORE | BASHOPTS | SHELLOPTS)
      bad_env=1
      ;;
    JAVA_TOOL_OPTIONS | _JAVA_OPTIONS | CLASSPATH)
      bad_env=1
      ;;
    PERLIO_DEBUG | PERLLIB | PERL5LIB | PERL5OPT | PERL5DB)
      bad_env=1
      ;;
    FPATH | NULLCMD | READNULLCMD | ZDOTDIR | TMPPREFIX)
      bad_env=1
      ;;
    PYTHONHOME | PYTHONPATH | PYTHONINSPECT | PYTHONUSERBASE | PYTHONSTARTUP)
      bad_env=1
      ;;
    RUBYLIB | RUBYOPT | NODE_OPTIONS | NODE_PATH | GIT_SSH_COMMAND | GIT_CONFIG_* | GCONV_PATH)
      bad_env=1
      ;;
    *) ;;
  esac
}

# Check if a variable needs character sanitization (% and / checks)
# Sets check_env=1 when the value must be rejected for this variable, else 0.
is_check_env() {
  var_name="$1"
  var_val="$2"
  check_env=0

  case "$var_name" in
    COLORTERM | LANG | LANGUAGE | LC_* | LINGUAS | TERM | TZ)
      case "$var_val" in
        *%* | */*) check_env=1 ;;
        *) ;;
      esac
      ;;
    *) ;;
  esac
}

# Process each KEY=VALUE pair
retained=""
stripped=""

for item in "$@"; do
  case "$item" in
    *=*)
      key="${item%%=*}"
      val="${item#*=}"
      ;;
    *)
      continue
      ;;
  esac

  is_bad_env "$key" "$val"
  is_check_env "$key" "$val"
  if [ "$bad_env" -eq 1 ]; then
    stripped="${stripped} ${key}"
  elif [ "$check_env" -eq 1 ]; then
    stripped="${stripped} ${key}(tainted)"
  elif [ "$key" = "PATH" ]; then
    # PATH is reset to secure_path
    retained="${retained} PATH=${SECURE_PATH}"
  else
    retained="${retained} ${key}=${val}"
  fi
done

printf 'SECURE_PATH: %s\n' "$SECURE_PATH"
printf 'STRIPPED:%s\n' "$stripped"
printf 'RETAINED:%s\n' "$retained"
