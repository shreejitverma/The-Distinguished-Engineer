#!/bin/sh
# tzlab.sh: probes for the date and Time Zones note.
#
# POSIX date(1) can only print the current time, so every probe that needs a
# fixed instant goes through a per-implementation wrapper (GNU -d @N, BSD -r N,
# BusyBox -d @N). The pure-arithmetic modes need no date(1) at all.
#
# Modes:
#   impl                       print gnu, uutils, busybox, bsd, or unknown
#   libc                       print glibc, musl, darwin, or unknown
#   fmt EPOCH FORMAT           format EPOCH (seconds) in the current TZ
#   parse DATETIME FORMAT      read "YYYY-MM-DD hh:mm:ss" as local time in TZ
#   add DATETIME N UNIT FORMAT add N days, hours, months, or years to DATETIME
#   now-check                  print date's %s and the XBD 4.19 formula value
#                              for one snapshot of the current UTC time
#   posix YEAR YDAY H M S      XBD 4.19 seconds since the Epoch (YDAY from 0)
#   epoch Y M D H M S          civil UTC time -> seconds (days_from_civil)
#   civil EPOCH                seconds -> "YYYY-MM-DD hh:mm:ss" UTC (civil_from_days)
#   strip NUMBER               remove leading zeros, keeping a lone 0

usage() {
  printf 'usage: %s MODE [ARGS]\n' "$0" >&2
  exit 2
}

# strip0 NUMBER: "008" -> "8", "000" -> "0"; arithmetic would read 008 as bad octal.
strip0() {
  v=$1
  while :; do
    case $v in
      0?*) v=${v#0} ;;
      *) break ;;
    esac
  done
  printf '%s\n' "$v"
}

impl() {
  out=$(date --version 2>&1 </dev/null) || :
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: date"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

libc() {
  if getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
    echo glibc
    return
  fi
  kernel=$(uname -s) || kernel=unknown
  if [ "$kernel" = Darwin ]; then
    echo darwin
    return
  fi
  out=$(ldd --version 2>&1 </dev/null) || :
  case $out in
    *musl*) echo musl ;;
    *) echo unknown ;;
  esac
}

# fmt EPOCH FORMAT
fmt() {
  kind=$(impl)
  case $kind in
    gnu | uutils | busybox) date -d "@$1" "+$2" ;;
    bsd) date -r "$1" "+$2" ;;
    *)
      echo "tzlab: unknown date implementation" >&2
      return 3
      ;;
  esac
}

# parse DATETIME FORMAT: DATETIME is "YYYY-MM-DD hh:mm:ss" in the zone named by TZ.
parse() {
  kind=$(impl)
  case $kind in
    gnu | uutils | busybox) date -d "$1" "+$2" ;;
    bsd) date -j -f '%Y-%m-%d %H:%M:%S' "$1" "+$2" ;;
    *)
      echo "tzlab: unknown date implementation" >&2
      return 3
      ;;
  esac
}

# add DATETIME N UNIT FORMAT: UNIT is day, hour, month, or year.
# GNU and BusyBox are both handed GNU's relative-item syntax; BusyBox rejects it.
add() {
  kind=$(impl)
  case $kind in
    gnu | uutils | busybox) date -d "$1 $2 $3" "+$4" ;;
    bsd)
      case $3 in
        day) unit=d ;;
        hour) unit=H ;;
        month) unit=m ;;
        year) unit=y ;;
        *) usage ;;
      esac
      date -j -v "+$2$unit" -f '%Y-%m-%d %H:%M:%S' "$1" "+$4"
      ;;
    *)
      echo "tzlab: unknown date implementation" >&2
      return 3
      ;;
  esac
}

# posix_seconds YEAR YDAY H M S: the expression in POSIX.1-2024 XBD 4.19, verbatim.
posix_seconds() {
  tm_year=$(($1 - 1900))
  tm_yday=$2
  tm_hour=$3
  tm_min=$4
  tm_sec=$5
  clock=$((tm_sec + tm_min * 60 + tm_hour * 3600 + tm_yday * 86400))
  years=$(((tm_year - 70) * 31536000 + ((tm_year - 69) / 4) * 86400))
  leap=$((((tm_year + 299) / 400) * 86400 - ((tm_year - 1) / 100) * 86400))
  echo $((clock + years + leap))
}

now_check() {
  # One date(1) call, so %s and the broken-down fields describe the same second.
  snap=$(date -u '+%s %Y %j %H %M %S') || return 1
  read -r s y j h mi sec <<EOF
$snap
EOF
  y=$(strip0 "$y")
  j=$(strip0 "$j")
  h=$(strip0 "$h")
  mi=$(strip0 "$mi")
  sec=$(strip0 "$sec")
  formula=$(posix_seconds "$y" $((j - 1)) "$h" "$mi" "$sec")
  printf '%s %s\n' "$s" "$formula"
}

# days_from_civil and civil_from_days follow Howard Hinnant's published algorithms.
epoch_from_civil() {
  y=$(strip0 "$1")
  m=$(strip0 "$2")
  d=$(strip0 "$3")
  h=$(strip0 "$4")
  mi=$(strip0 "$5")
  s=$(strip0 "$6")
  if [ "$m" -le 2 ]; then y=$((y - 1)); fi
  if [ "$y" -ge 0 ]; then era=$((y / 400)); else era=$(((y - 399) / 400)); fi
  yoe=$((y - era * 400))
  if [ "$m" -gt 2 ]; then mm=$((m - 3)); else mm=$((m + 9)); fi
  doy=$(((153 * mm + 2) / 5 + d - 1))
  doe=$((yoe * 365 + yoe / 4 - yoe / 100 + doy))
  days=$((era * 146097 + doe - 719468))
  echo $((days * 86400 + h * 3600 + mi * 60 + s))
}

civil_from_epoch() {
  t=$1
  days=$((t / 86400))
  secs=$((t % 86400))
  # Shell division truncates toward zero; floor it for instants before 1970.
  if [ "$secs" -lt 0 ]; then
    days=$((days - 1))
    secs=$((secs + 86400))
  fi
  z=$((days + 719468))
  if [ "$z" -ge 0 ]; then era=$((z / 146097)); else era=$(((z - 146096) / 146097)); fi
  doe=$((z - era * 146097))
  yoe=$(((doe - doe / 1460 + doe / 36524 - doe / 146096) / 365))
  y=$((yoe + era * 400))
  doy=$((doe - (365 * yoe + yoe / 4 - yoe / 100)))
  mp=$(((5 * doy + 2) / 153))
  d=$((doy - (153 * mp + 2) / 5 + 1))
  if [ "$mp" -lt 10 ]; then m=$((mp + 3)); else m=$((mp - 9)); fi
  if [ "$m" -le 2 ]; then y=$((y + 1)); fi
  printf '%04d-%02d-%02d %02d:%02d:%02d\n' "$y" "$m" "$d" \
    $((secs / 3600)) $((secs % 3600 / 60)) $((secs % 60))
}

# need GOT WANT: exit with usage unless the mode got WANT arguments.
need() {
  [ "$1" -eq "$2" ] || usage
}

[ "$#" -ge 1 ] || usage
mode=$1
shift
case $mode in
  impl) impl ;;
  libc) libc ;;
  fmt)
    need "$#" 2
    fmt "$1" "$2"
    ;;
  parse)
    need "$#" 2
    parse "$1" "$2"
    ;;
  add)
    need "$#" 4
    add "$1" "$2" "$3" "$4"
    ;;
  now-check) now_check ;;
  posix)
    need "$#" 5
    posix_seconds "$1" "$2" "$3" "$4" "$5"
    ;;
  epoch)
    need "$#" 6
    epoch_from_civil "$1" "$2" "$3" "$4" "$5" "$6"
    ;;
  civil)
    need "$#" 1
    civil_from_epoch "$1"
    ;;
  strip)
    need "$#" 1
    strip0 "$1"
    ;;
  *) usage ;;
esac
