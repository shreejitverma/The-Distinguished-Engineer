#!/usr/bin/env bats
# Tests for Mounts and Mount Namespaces: mountinfo parsing, df -P, EXDEV, and namespace isolation.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/mounts.sh"
  FIXTURE="$BATS_TEST_DIRNAME/mountinfo.fixture"
  # The kernel decides which mount facilities exist: Linux has mountinfo and namespaces, macOS has neither.
  case $(uname -s) in
    Linux) OS=linux ;;
    Darwin) OS=darwin ;;
    *) false ;;
  esac
}

@test "mountinfo octal escapes decode to space, tab, backslash, and a trailing newline" {
  run_lab "$PROBE" decode 'a\040b\011c\134d\012'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '<a b\tc\\d\n>')" ]
  [ "${#output}" -eq 10 ]
}

@test "a mount point with an escaped space serves paths below it" {
  run_lab "$PROBE" lookup '/mnt/my disk/photos' "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=30 point=</mnt/my disk> fstype=ext4 propagation=shared:7" ]
}

@test "a string prefix that is not a directory prefix does not match the mount" {
  run_lab "$PROBE" lookup '/mnt/my diskette' "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=21 point=</> fstype=ext4 propagation=shared:1" ]
}

@test "the top-most of three stacked mounts wins even when listed out of order" {
  run_lab "$PROBE" lookup /srv/x "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=42 point=</srv> fstype=tmpfs propagation=private" ]
}

@test "the longest mount point wins and its unbindable tag is reported" {
  run_lab "$PROBE" lookup /srv/data/f "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=50 point=</srv/data> fstype=ext4 propagation=unbindable" ]
}

@test "the proc mount is found with its shared peer group" {
  run_lab "$PROBE" lookup /proc/self "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=22 point=</proc> fstype=proc propagation=shared:12" ]
}

@test "a backslash in a mount point is matched after decoding" {
  run_lab "$PROBE" lookup '/back\slash/file' "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "id=60 point=</back\\slash> fstype=ext4 propagation=private" ]
}

@test "the live mount table puts /proc on a proc mount on Linux and is absent on macOS" {
  run_lab "$PROBE" live /proc/self
  [ "$status" -eq 0 ]
  case $OS in
    linux)
      case $output in
        "id="*" point=</proc> fstype=proc propagation="*) ;;
        *) false ;;
      esac
      ;;
    darwin) [ "$output" = "mountinfo: unavailable" ] ;;
    *) false ;;
  esac
}

@test "df -P names / as the mount serving /" {
  run_lab "$PROBE" dfroot /
  [ "$status" -eq 0 ]
  [ "$output" = "/" ]
}

@test "df -P names the mount serving the test directory; on macOS it is not a path prefix" {
  physical=$(cd "$BATS_TEST_TMPDIR" && pwd -P)
  run_lab "$PROBE" dfroot "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ -n "$output" ]
  case $OS in
    linux)
      # On Linux the serving mount point is always a directory prefix of the physical path.
      case $physical in
        "$output" | "$output"/*) ;;
        *) [ "$output" = / ] ;;
      esac
      ;;
    darwin)
      # macOS: /private/var and /Users are firmlinks into the Data volume, so the mount point
      # (/System/Volumes/Data) is not a prefix of the path the kernel reports.
      [ "$output" = /System/Volumes/Data ]
      case $physical in
        /System/Volumes/Data/*) false ;;
        *) ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "link(2) from another mount: EXDEV on Linux, EPERM first on macOS; same-directory link succeeds" {
  "${CC:-cc}" -Wall -Wextra -o "$BATS_TEST_TMPDIR/xdev_link" "$BATS_TEST_DIRNAME/xdev_link.c"
  # /dev is its own mount (devtmpfs or tmpfs on Linux, devfs on macOS), never the test directory's.
  run "$BATS_TEST_TMPDIR/xdev_link" /dev/null "$BATS_TEST_TMPDIR/null-link"
  [ "$status" -eq 1 ]
  case $OS in
    linux) [ "$output" = "link=EXDEV" ] ;;  # filename_linkat compares mounts before permissions
    darwin) [ "$output" = "link=EPERM" ] ;; # linkat_internal authorizes the source before EXDEV
    *) false ;;
  esac
  [ ! -e "$BATS_TEST_TMPDIR/null-link" ]
  printf 'data\n' >"$BATS_TEST_TMPDIR/a"
  run "$BATS_TEST_TMPDIR/xdev_link" "$BATS_TEST_TMPDIR/a" "$BATS_TEST_TMPDIR/b"
  [ "$status" -eq 0 ]
  [ "$output" = "link=ok" ]
  [ "$BATS_TEST_TMPDIR/a" -ef "$BATS_TEST_TMPDIR/b" ]
}

@test "a forked child shares its parent's mount namespace" {
  "${CC:-cc}" -Wall -Wextra -o "$BATS_TEST_TMPDIR/mntns" "$BATS_TEST_DIRNAME/mntns.c"
  run "$BATS_TEST_TMPDIR/mntns" inherit
  case $OS in
    linux)
      [ "$status" -eq 0 ]
      [ "$output" = "child_shares_parent_mntns=yes" ]
      ;;
    darwin)
      [ "$status" -eq 0 ]
      [ "$output" = "mntns: unsupported" ]
      ;;
    *) false ;;
  esac
}

@test "a tmpfs mounted in a new mount namespace is invisible to the parent namespace" {
  "${CC:-cc}" -Wall -Wextra -o "$BATS_TEST_TMPDIR/mntns" "$BATS_TEST_DIRNAME/mntns.c"
  mkdir "$BATS_TEST_TMPDIR/scratch"
  run "$BATS_TEST_TMPDIR/mntns" isolate "$BATS_TEST_TMPDIR/scratch"
  case $OS in
    linux)
      # Whether an unprivileged user may create a user namespace is host policy (container
      # seccomp profiles, kernel.apparmor_restrict_unprivileged_userns, max_user_namespaces=0).
      # The probe names the stage that was refused; where nothing is refused, isolation must hold.
      case $output in
        "mntns=new child_sees=yes parent_sees=no") [ "$status" -eq 0 ] ;;
        "failed_at="*" errno="*" parent_sees=no")
          [ "$status" -eq 3 ]
          case $output in
            failed_at=unshare\ errno=EPERM* | failed_at=unshare\ errno=ENOSPC* | failed_at=unshare\ errno=EUSERS*) ;;
            failed_at=uid_map\ errno=EPERM* | failed_at=setgroups\ errno=EPERM* | failed_at=gid_map\ errno=EPERM*) ;;
            failed_at=make-rprivate\ errno=EPERM* | failed_at=mount\ errno=EPERM* | failed_at=mount\ errno=EACCES*) ;;
            *) false ;;
          esac
          ;;
        *) false ;;
      esac
      [ ! -e "$BATS_TEST_TMPDIR/scratch/inside" ]
      ;;
    darwin)
      [ "$status" -eq 0 ]
      [ "$output" = "mntns: unsupported" ]
      ;;
    *) false ;;
  esac
}
