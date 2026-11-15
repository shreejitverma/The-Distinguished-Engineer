#!/usr/bin/env bats
# Tests for the rsync delta-transfer model: weak checksum, rolling update,
# block-size heuristic, and the signature -> delta -> patch flow.

setup() {
  load ../lib/lab
  TOOL="${BATS_TEST_DIRNAME}/rsync_lab.sh"
  W="${BATS_TEST_TMPDIR}"
}

# The model's results do not depend on the shell, but an unknown family must fail loudly.
known_family() {
  local family
  family=$(lab_shell_family)
  case "${family}" in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) ;;
    *) false ;;
  esac
}

# expect KEY VALUE: the last run printed the line KEY=VALUE.
expect() {
  local line
  for line in "${lines[@]}"; do
    if [ "${line}" = "$1=$2" ]; then
      return 0
    fi
  done
  printf 'expected %s=%s in:\n%s\n' "$1" "$2" "${output}" >&2
  return 1
}

@test "weak checksum of abc is a=294 plus 2^16 times b=586" {
  known_family
  printf 'abc' >"${W}/abc"
  run_lab "${TOOL}" "${W}" weak "${W}/abc"
  [ "${status}" -eq 0 ]
  # a = 97+98+99 = 294; b = 3*97 + 2*98 + 1*99 = 586; 586*65536 + 294 = 38404390.
  expect weak 38404390
}

@test "weak checksum reads bytes as signed char, so 0xff contributes -1" {
  known_family
  printf '\377' >"${W}/ff"
  run_lab "${TOOL}" "${W}" weak "${W}/ff"
  [ "${status}" -eq 0 ]
  # Signed: s1 = s2 = -1, giving 0xffffffff. Unsigned would give 255 + 255*65536 = 16711935.
  expect weak 4294967295
}

@test "aaa and b_b collide in the weak checksum but not in the strong one" {
  known_family
  printf 'aaa' >"${W}/x"
  printf 'b_b' >"${W}/y"
  run_lab "${TOOL}" "${W}" weak "${W}/x"
  [ "${status}" -eq 0 ]
  expect weak 38142243
  run_lab "${TOOL}" "${W}" weak "${W}/y"
  [ "${status}" -eq 0 ]
  expect weak 38142243
  run_lab "${TOOL}" "${W}" strong "${W}/x"
  [ "${status}" -eq 0 ]
  sx=${output}
  run_lab "${TOOL}" "${W}" strong "${W}/y"
  [ "${status}" -eq 0 ]
  [ "${output}" != "${sx}" ]
}

@test "rolling update equals a fresh checksum at every one of 64837 offsets" {
  known_family
  run_lab "${TOOL}" "${W}" gen 7 65536 "${W}/r"
  [ "${status}" -eq 0 ]
  size=$(wc -c <"${W}/r")
  [ "${size}" -eq 65536 ]
  run_lab "${TOOL}" "${W}" roll "${W}/r" 700
  [ "${status}" -eq 0 ]
  expect windows 64837
  expect mismatches 0
}

@test "block length is sqrt of file size rounded down to a multiple of 8, 700 to 131072" {
  known_family
  run_lab "${TOOL}" "${W}" sizes 100000
  [ "${status}" -eq 0 ]
  expect blength 700
  expect s2length 2
  expect count 143
  run_lab "${TOOL}" "${W}" sizes 1048576
  [ "${status}" -eq 0 ]
  expect blength 1024
  expect s2length 2
  expect sig_wire_bytes 6144
  run_lab "${TOOL}" "${W}" sizes 4000000
  [ "${status}" -eq 0 ]
  expect blength 2000
  expect s2length 2
  expect sig_wire_bytes 12000
  run_lab "${TOOL}" "${W}" sizes 1073741824
  [ "${status}" -eq 0 ]
  expect blength 32768
  expect s2length 3
  expect sig_wire_bytes 229376
  run_lab "${TOOL}" "${W}" sizes 107374182400
  [ "${status}" -eq 0 ]
  expect blength 131072
  expect s2length 5
  expect count 819200
  expect sig_wire_bytes 7372800
}

@test "one prepended byte: rsync matches every block, fixed offsets match none" {
  known_family
  run_lab "${TOOL}" "${W}" insert
  [ "${status}" -eq 0 ]
  expect matched_blocks 1024
  expect literal_bytes 1
  expect false_alarms 0
  expect delta_bytes 5130
  expect rebuilt identical
  expect aligned_matches 0
  expect new_blocks 1025
}

@test "one overwritten byte costs exactly one literal block" {
  known_family
  run_lab "${TOOL}" "${W}" overwrite
  [ "${status}" -eq 0 ]
  expect matched_blocks 1023
  expect literal_bytes 1024
  expect delta_bytes 6148
  expect rebuilt identical
  expect aligned_matches 1023
}

@test "swapped halves: matches do not depend on block position" {
  known_family
  run_lab "${TOOL}" "${W}" swap
  [ "${status}" -eq 0 ]
  expect matched_blocks 1024
  expect literal_bytes 0
  expect delta_bytes 5124
  expect rebuilt identical
  expect aligned_matches 0
}

@test "a weak-checksum collision is a false alarm, never a wrong copy" {
  known_family
  run_lab "${TOOL}" "${W}" collide
  [ "${status}" -eq 0 ]
  expect hash_hits 1
  expect false_alarms 1
  expect matched_blocks 0
  expect literal_bytes 3
  expect rebuilt identical
}
