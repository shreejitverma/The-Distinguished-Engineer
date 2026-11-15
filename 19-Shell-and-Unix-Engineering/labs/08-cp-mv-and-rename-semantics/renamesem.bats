#!/usr/bin/env bats
# cp, mv, and rename semantics: what rename(2) guarantees, and where the GNU, BSD and
# macOS, and BusyBox cp and mv implementations add policy on top of it.
# The shell under test runs the probe; the cp and mv on PATH decide the behavior, so the
# tests branch on the utility implementation, not on the shell family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/renamesem.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# util_flavor CMD: print gnu, busybox, uutils, or bsd for the cp or mv on PATH, or unknown.
util_flavor() {
  local out
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: $1"*) echo bsd ;; # BSD and macOS reject --version and print their usage
    *) echo unknown ;;
  esac
}

# gnu_minor: print the GNU coreutils version of cp as MAJOR*100+MINOR (9.12 -> 912).
gnu_minor() {
  local out line version major minor
  out=$(cp --version 2>/dev/null) || return 1
  line=$(printf "%s\n" "$out" | head -n 1)
  version=${line##* }
  major=${version%%.*}
  minor=${version#*.}
  minor=${minor%%.*}
  echo $((major * 100 + minor))
}

# build_probe: compile rename_probe.c into the test directory and print its path.
build_probe() {
  "${CC:-cc}" -Wall -Wextra -Werror -o "$BATS_TEST_TMPDIR/rename_probe" \
    "$BATS_TEST_DIRNAME/rename_probe.c"
  printf '%s\n' "$BATS_TEST_TMPDIR/rename_probe"
}

@test "rename replaces the name only: an open reader keeps the old inode and its bytes" {
  run_lab "$PROBE" mv-reader
  [ "$status" -eq 0 ]
  [ "$output" = "rc=0 fd=OLD name=NEW inode_is_new=yes" ]
}

@test "cp onto an existing file truncates it in place (GNU, BSD) or replaces it (BusyBox)" {
  run_lab "$PROBE" cp-reader
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu | bsd) [ "$output" = "rc=0 fd=new link=new same_inode=yes mode=-rw-------" ] ;;
    busybox) [ "$output" = "rc=0 fd=old link=old same_inode=no mode=-rw-r--r--" ] ;;
    *) false ;;
  esac
}

@test "a new copy gets the source mode minus the umask; cp -p keeps the source mode" {
  run_lab "$PROBE" umask
  [ "$status" -eq 0 ]
  [ "$output" = "plain=-rw------- kept=-rw-r--r--" ]
}

@test "cp -p copies the modification time; plain cp stamps the copy with the current time" {
  run_lab "$PROBE" preserve-time
  [ "$status" -eq 0 ]
  [ "$output" = "plain_newer=plain kept_newer=none" ]
}

@test "cp a a always fails; mv a a fails in GNU and succeeds as a no-op elsewhere" {
  run_lab "$PROBE" same-file
  [ "$status" -eq 0 ]
  case $(util_flavor mv) in
    gnu) [ "$output" = "cp_rc=1 mv_rc=1 content=x" ] ;;
    bsd | busybox) [ "$output" = "cp_rc=1 mv_rc=0 content=x" ] ;;
    *) false ;;
  esac
}

@test "mv between two hard links of one file keeps both names; only GNU reports an error" {
  run_lab "$PROBE" hardlinks
  [ "$status" -eq 0 ]
  case $(util_flavor mv) in
    gnu) [ "$output" = "rc=1 h1=yes h2=yes" ] ;;
    bsd | busybox) [ "$output" = "rc=0 h1=yes h2=yes" ] ;;
    *) false ;;
  esac
}

@test "rename(2) itself succeeds and does nothing when both names are one file" {
  probe=$(build_probe)
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/rp.XXXXXX")
  cd "$dir"
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'printf h >h1; ln h1 h2; "$1" plain h1 h2; ls h1 h2' sh "$probe"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'ok\nh1\nh2')" ]
}

@test "the no-replace flag makes the existence check and the rename one atomic step" {
  probe=$(build_probe)
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/rp.XXXXXX")
  cd "$dir"
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'printf a >a; printf b >b; "$1" noreplace a b; cat b; echo; "$1" noreplace a c; cat c' sh "$probe"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'EEXIST\nb\nok\na')" ]
}

@test "the exchange flag swaps two names atomically and needs both to exist" {
  probe=$(build_probe)
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/rp.XXXXXX")
  cd "$dir"
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'printf 1 >e1; printf 2 >e2; "$1" exchange e1 e2; cat e1 e2; echo; "$1" exchange e1 nope' sh "$probe"
  [ "$status" -eq 1 ]
  [ "$output" = "$(printf 'ok\n21\nENOENT')" ]
}

@test "cp -R src/ into an existing directory: BSD copies the contents, GNU and BusyBox the directory" {
  run_lab "$PROBE" cp-tree-slash
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "into=[./s ./s/f ./s/sub]" ]
  case $(util_flavor cp) in
    gnu | busybox) [ "${lines[1]}" = "into_slash=[./s ./s/f ./s/sub]" ] ;;
    bsd) [ "${lines[1]}" = "into_slash=[./f ./sub]" ] ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "fresh=[./f ./sub]" ]
}

@test "the same mv renames the first time and nests the second time" {
  run_lab "$PROBE" mv-twice
  [ "$status" -eq 0 ]
  [ "$output" = "out=[./build ./build/f ./f]" ]
}

@test "mv refuses to replace a non-empty directory and leaves the source in place" {
  run_lab "$PROBE" mv-nonempty
  [ "$status" -eq 0 ]
  [ "$output" = "rc=1 x=yes y_x_q=no" ]
}

@test "mv refuses to move a directory into its own subdirectory" {
  run_lab "$PROBE" mv-into-self
  [ "$status" -eq 0 ]
  [ "$output" = "rc=1 md=yes sub=no" ]
}

@test "a trailing slash on a non-existent target makes mv and cp of a file fail" {
  run_lab "$PROBE" trailing-slash
  [ "$status" -eq 0 ]
  [ "$output" = "mv_rc=1 cp_rc=1 t=yes nodir=no nodir2=no" ]
}

@test "mv replaces a symlink; cp writes through it (GNU, BSD) or replaces it (BusyBox)" {
  run_lab "$PROBE" symlink-dest
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu | bsd) [ "$output" = "mv_rc=0 mv_link=no mv_content=M cp_rc=0 cp_link=yes target=C" ] ;;
    busybox) [ "$output" = "mv_rc=0 mv_link=no mv_content=M cp_rc=0 cp_link=no target=T" ] ;;
    *) false ;;
  esac
}

@test "cp onto a dangling symlink: GNU refuses, BSD creates the target, BusyBox replaces the link" {
  run_lab "$PROBE" dangling
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu) [ "$output" = "rc=1 dang_link=yes missing=absent" ] ;;
    bsd) [ "$output" = "rc=0 dang_link=yes missing=C" ] ;;
    busybox) [ "$output" = "rc=0 dang_link=no missing=absent" ] ;;
    *) false ;;
  esac
}

@test "POSIXLY_CORRECT makes GNU cp write through a dangling symlink like BSD" {
  run_lab "$PROBE" dangling-posix
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu | bsd) [ "$output" = "rc=0 dang_link=yes missing=C" ] ;;
    busybox) [ "$output" = "rc=0 dang_link=no missing=absent" ] ;;
    *) false ;;
  esac
}

@test "-n never overwrites, but its exit status differs by implementation and version" {
  run_lab "$PROBE" no-clobber
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu)
      v=$(gnu_minor)
      if [ "$v" -ge 902 ] && [ "$v" -le 904 ]; then
        [ "$output" = "mv_rc=1 cp_rc=1 b=B a=yes" ]
      else
        [ "$output" = "mv_rc=0 cp_rc=0 b=B a=yes" ]
      fi
      ;;
    bsd) [ "$output" = "mv_rc=0 cp_rc=1 b=B a=yes" ] ;;
    busybox) [ "$output" = "mv_rc=0 cp_rc=0 b=B a=yes" ] ;;
    *) false ;;
  esac
}

@test "mv --exchange swaps two files in GNU 9.5 and later and is rejected elsewhere" {
  run_lab "$PROBE" exchange
  [ "$status" -eq 0 ]
  case $(util_flavor mv) in
    gnu)
      v=$(gnu_minor)
      if [ "$v" -ge 905 ]; then
        [ "$output" = "rc=0 e1=2 e2=1" ]
      else
        [ "$output" = "rc=1 e1=1 e2=2" ]
      fi
      ;;
    bsd) [ "$output" = "rc=64 e1=1 e2=2" ] ;;
    busybox) [ "$output" = "rc=1 e1=1 e2=2" ] ;;
    *) false ;;
  esac
}

@test "atomic_write replaces the file with a new inode, sets its mode, and leaves no temp file" {
  run_lab "$PROBE" atomic-write
  [ "$status" -eq 0 ]
  [ "$output" = "rc=0 fd=OLD name=NEW same_inode=no mode=-rw-r--r-- leftovers=none" ]
}

@test "atomic_write keeps the old file when the write fails and refuses a directory target" {
  run_lab "$PROBE" atomic-write-fail
  [ "$status" -eq 0 ]
  [ "$output" = "rc=1 name=OLD dir_rc=1 adir_entries=[] leftovers=none" ]
}
