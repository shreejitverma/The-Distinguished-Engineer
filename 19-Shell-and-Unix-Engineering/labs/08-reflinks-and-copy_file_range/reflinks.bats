#!/usr/bin/env bats
# Reflink clones and copy_file_range: what the kernel and each cp actually do.
# Expectations branch on the file system under $BATS_TEST_TMPDIR (reported by the probe)
# and on the cp implementation, never on the shell: the shell only launches the programs.

setup() {
  load ../lib/lab
  TOOL="${BATS_TEST_DIRNAME}/reflink_tool.sh"
  PROBE_SRC="${BATS_TEST_DIRNAME}/probe.c"
  PROBE="${BATS_TEST_TMPDIR}/probe"
  D="${BATS_TEST_TMPDIR}/d"
  mkdir -p "${D}"
}

# Every shell family in the matrix; an unknown family fails the test instead of passing silently.
require_known_family() {
  family=$(lab_shell_family)
  case "${family}" in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) ;;
    *) false ;;
  esac
}

build_probe() {
  run_lab "${TOOL}" compile "${PROBE_SRC}" "${PROBE}"
  [ "${status}" -eq 0 ]
  [ -x "${PROBE}" ]
}

# probe MODE ARG...: run the compiled probe through the shell under test.
probe() {
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c '"$@"' sh "${PROBE}" "$@"
}

detect_fs() {
  probe fstype "${D}"
  [ "${status}" -eq 0 ]
  FS=${output#fstype }
  if [ "${FS}" = overlay ]; then
    # overlayfs forwards clones to its upper file system, so classify it by what a clone does.
    printf 'x' >"${D}/.overlay-src"
    probe clone "${D}/.overlay-src" "${D}/.overlay-dst"
    if [ "${output}" = "clone ok" ]; then
      FS=overlay-reflink
    fi
    rm -f "${D}/.overlay-src" "${D}/.overlay-dst"
  fi
}

# make_payload FILE BYTE MIB: write a repeated byte across many file system blocks.
make_payload() {
  awk -v byte="$2" -v mib="$3" 'BEGIN {
    chunk = byte
    for (i = 0; i < 10; i++) chunk = chunk chunk
    for (i = 0; i < mib * 1024; i++) printf "%s", chunk
  }' >"$1"
}

@test "probe compiles with the C compiler" {
  require_known_family
  build_probe
  probe fstype "${D}"
  [ "${status}" -eq 0 ]
  [[ "${output}" == "fstype "* ]]
}

@test "a clone is a separate file whose writes stay private (copy on write)" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  detect_fs
  fs=${FS}
  probe clone "${D}/src" "${D}/clone"
  [ "${status}" -eq 0 ]
  case "${fs}" in
    apfs | btrfs | xfs | overlay-reflink)
      [ "${output}" = "clone ok" ]
      cmp "${D}/src" "${D}/clone"
      src_ino=$(ls -i "${D}/src")
      clone_ino=$(ls -i "${D}/clone")
      [ "${src_ino%% *}" != "${clone_ino%% *}" ]
      # Overwrite the first byte of the clone in place.
      printf 'Z' | dd of="${D}/clone" bs=1 seek=0 conv=notrunc 2>/dev/null
      src_first=$(head -c 1 "${D}/src")
      clone_first=$(head -c 1 "${D}/clone")
      [ "${src_first}" = a ]
      [ "${clone_first}" = Z ]
      # The written block is now private; the rest is still shared.
      probe shared "${D}/clone"
      [ "${output}" = "shared some" ]
      ;;
    ext4 | tmpfs | overlay)
      # No remap_file_range method: vfs_clone_file_range returns -EOPNOTSUPP.
      # overlay here means an upper layer that cannot clone (for example ext4); detect_fs maps an
      # overlay whose upper layer clones (XFS with reflink, Btrfs) to overlay-reflink instead.
      [ "${output}" = "clone EOPNOTSUPP" ]
      ;;
    *) false ;;
  esac
}

@test "a clone shares all its blocks; a read-write copy shares none" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  cat "${D}/src" >"${D}/plain"
  detect_fs
  fs=${FS}
  probe shared "${D}/plain"
  case "${fs}" in
    apfs | btrfs | xfs | overlay-reflink)
      if [ "${fs}" != overlay-reflink ]; then
        # FIEMAP through a forwarding overlay reported even a fresh read-write copy as shared
        # (measured on OrbStack, Linux 7.2.12), so only the clone side is asserted there.
        [ "${output}" = "shared none" ]
      fi
      probe clone "${D}/src" "${D}/clone"
      [ "${output}" = "clone ok" ]
      probe shared "${D}/clone"
      [ "${output}" = "shared all" ]
      ;;
    ext4 | overlay)
      [ "${output}" = "shared none" ]
      ;;
    tmpfs)
      # tmpfs has no fiemap method, so FS_IOC_FIEMAP itself fails.
      [ "${output}" = "shared EOPNOTSUPP" ]
      ;;
    *) false ;;
  esac
}

@test "cloning onto an existing destination: EEXIST on APFS, replace on Linux" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  printf 'old\n' >"${D}/dst"
  detect_fs
  fs=${FS}
  probe clone "${D}/src" "${D}/dst"
  case "${fs}" in
    apfs)
      # clonefile(2) creates the destination itself and refuses an existing one.
      [ "${output}" = "clone EEXIST" ]
      dst_contents=$(cat "${D}/dst")
      [ "${dst_contents}" = old ]
      ;;
    btrfs | xfs | overlay-reflink)
      # FICLONE works on an open destination and replaces its contents.
      [ "${output}" = "clone ok" ]
      cmp "${D}/src" "${D}/dst"
      ;;
    ext4 | tmpfs | overlay)
      [ "${output}" = "clone EOPNOTSUPP" ]
      ;;
    *) false ;;
  esac
}

@test "copy_file_range copies a 3 MiB file in the kernel, or is absent on macOS" {
  require_known_family
  build_probe
  make_payload "${D}/src" b 3
  sysname=$(uname -s)
  probe cfr "${D}/src" "${D}/dst"
  [ "${status}" -eq 0 ]
  case "${sysname}" in
    Darwin)
      # No copy_file_range symbol in libSystem; macOS offers clonefile and copyfile instead.
      [ "${output}" = "cfr absent" ]
      ;;
    Linux)
      [[ "${output}" == "cfr ok bytes=3145728 calls="* ]]
      calls=${output##*calls=}
      # At least one call that copies and one that returns 0 at end of file.
      [ "${calls}" -ge 2 ]
      cmp "${D}/src" "${D}/dst"
      ;;
    *) false ;;
  esac
}

@test "copy_file_range from /proc fails with EXDEV instead of copying zero bytes" {
  require_known_family
  build_probe
  sysname=$(uname -s)
  case "${sysname}" in
    Darwin)
      [ ! -e /proc/self/status ]
      probe cfr "${D}/missing" "${D}/dst"
      [ "${output}" = "cfr absent" ]
      ;;
    Linux)
      # The file has content but reports st_size 0.
      [ ! -s /proc/self/status ]
      proc_bytes=$(wc -c </proc/self/status)
      [ "${proc_bytes}" -gt 0 ]
      probe cfr /proc/self/status "${D}/dst"
      # Since Linux 5.19 a cross-file-system copy without a shared ->copy_file_range is EXDEV.
      [ "${output}" = "cfr EXDEV bytes=0" ]
      [ ! -s "${D}/dst" ]
      ;;
    *) false ;;
  esac
}

@test "copy_file_range rejects an O_APPEND destination with EBADF" {
  require_known_family
  build_probe
  printf 'data\n' >"${D}/src"
  sysname=$(uname -s)
  probe cfr-append "${D}/src" "${D}/dst"
  case "${sysname}" in
    Darwin) [ "${output}" = "cfr absent" ] ;;
    Linux) [ "${output}" = "cfr EBADF bytes=0" ] ;;
    *) false ;;
  esac
}

@test "cp --reflink=always fails instead of falling back to a copy" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  detect_fs
  fs=${FS}
  run_lab "${TOOL}" cp-flavor
  flavor=${output}
  # shellcheck disable=SC2016 # "$1" and "$2" belong to the shell under test
  run_lab_c 'cp --reflink=always "$1" "$2"' sh "${D}/src" "${D}/dst"
  case "${flavor}" in
    gnu | uutils)
      case "${fs}" in
        apfs | btrfs | xfs | overlay-reflink)
          [ "${status}" -eq 0 ]
          probe shared "${D}/dst"
          [ "${output}" = "shared all" ]
          ;;
        ext4 | tmpfs | overlay)
          [ "${status}" -eq 1 ]
          [[ "${output}" == *"failed to clone"* ]]
          # coreutils 9.2+ and uutils remove the destination they created.
          [ ! -e "${D}/dst" ]
          ;;
        *) false ;;
      esac
      ;;
    busybox)
      case "${output}" in
        *"unrecognized option"*)
          # Built without FEATURE_CP_REFLINK.
          [ "${status}" -eq 1 ]
          ;;
        *"failed to clone"*)
          # BusyBox reports the failure but leaves the empty destination behind.
          [ "${status}" -eq 1 ]
          [ -e "${D}/dst" ]
          [ ! -s "${D}/dst" ]
          ;;
        "")
          [ "${status}" -eq 0 ]
          cmp "${D}/src" "${D}/dst"
          ;;
        *) false ;;
      esac
      ;;
    bsd)
      # The BSD and macOS cp have no long options: a usage error, EX_USAGE (64).
      [ "${status}" -eq 64 ]
      [[ "${output}" == *"illegal option"* ]]
      [ ! -e "${D}/dst" ]
      ;;
    *) false ;;
  esac
}

@test "best-copy clones when the file system can and copies when it cannot" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  detect_fs
  fs=${FS}
  run_lab "${TOOL}" best-copy "${D}/src" "${D}/dst"
  [ "${status}" -eq 0 ]
  [[ "${output}" == "via: cp"* ]]
  cmp "${D}/src" "${D}/dst"
  probe shared "${D}/dst"
  case "${fs}" in
    apfs | btrfs | xfs | overlay-reflink) [ "${output}" = "shared all" ] ;;
    ext4 | overlay) [ "${output}" = "shared none" ] ;;
    tmpfs) [ "${output}" = "shared EOPNOTSUPP" ] ;;
    *) false ;;
  esac
}

@test "macOS cp clones only with -c; GNU cp clones by default" {
  require_known_family
  build_probe
  make_payload "${D}/src" a 1
  detect_fs
  fs=${FS}
  run_lab "${TOOL}" cp-flavor
  flavor=${output}
  # shellcheck disable=SC2016 # "$1" and "$2" belong to the shell under test
  run_lab_c 'cp "$1" "$2"' sh "${D}/src" "${D}/plain"
  [ "${status}" -eq 0 ]
  probe shared "${D}/plain"
  case "${flavor}:${fs}" in
    bsd:apfs)
      # Plain cp uses fcopyfile(3) without COPYFILE_CLONE: a real copy.
      [ "${output}" = "shared none" ]
      # shellcheck disable=SC2016 # "$1" and "$2" belong to the shell under test
      run_lab_c 'cp -c "$1" "$2"' sh "${D}/src" "${D}/clone"
      [ "${status}" -eq 0 ]
      probe shared "${D}/clone"
      [ "${output}" = "shared all" ]
      ;;
    gnu:apfs | gnu:btrfs | gnu:xfs | gnu:overlay-reflink | uutils:apfs | uutils:btrfs | uutils:xfs | uutils:overlay-reflink)
      # --reflink=auto is the default (coreutils 9.0+, uutils on Linux and macOS).
      [ "${output}" = "shared all" ]
      ;;
    busybox:btrfs | busybox:xfs | busybox:overlay-reflink)
      # BusyBox clones only when asked with --reflink.
      [ "${output}" = "shared none" ]
      ;;
    *:ext4 | *:overlay)
      [ "${output}" = "shared none" ]
      ;;
    *:tmpfs)
      [ "${output}" = "shared EOPNOTSUPP" ]
      ;;
    *) false ;;
  esac
}
