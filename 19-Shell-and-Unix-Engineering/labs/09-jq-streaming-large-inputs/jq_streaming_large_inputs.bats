#!/usr/bin/env bats
# jq_streaming_large_inputs.bats: the jq streaming parser (--stream) as a way to process JSON
# that does not fit in memory - event shapes, the fromstream trap, constant memory, early exit,
# error behavior, depth limits, --seq framing, and program injection through shell quoting.
# The shell under test only builds argv, redirections, and pipelines for jq; every behavior
# asserted here belongs to the jq binary found on PATH, so no test branches on the shell family.
# shellcheck disable=SC2016 # every run_lab_c string is expanded by the shell under test

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
  command -v jq >/dev/null 2>&1 || {
    echo "jq is not installed" >&2
    return 1
  }
  W=$BATS_TEST_TMPDIR
}

# build_peak: compile peak-rss.c into $W/peak-rss (a missing compiler fails the test).
build_peak() {
  run_lab_c '"${CC:-cc}" -O1 -o "$2/peak-rss" "$1/peak-rss.c"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
}

# peak_bytes JQ_ARG...: run jq with the arguments under peak-rss; set PEAK_BYTES, require status 0.
peak_bytes() {
  run_lab_c 'p=$1; shift; "$p/peak-rss" jq "$@"' inline "$W" "$@"
  [ "$status" -eq 0 ]
  PEAK_BYTES=${output%% *}
  [ "${output##* }" = 0 ]
}

# --- event shapes ----------------------------------------------------------------------------

@test "--stream emits [path,leaf] events, [path] closers, and empty containers as leaves" {
  run_lab_c 'printf "%s\n" "{\"a\":[1,{\"b\":2}],\"c\":[],\"d\":{}}" | jq -c --stream .'
  [ "$status" -eq 0 ]
  [ "$output" = $'[["a",0],1]\n[["a",1,"b"],2]\n[["a",1,"b"]]\n[["a",1]]\n[["c"],[]]\n[["d"],{}]\n[["d"]]' ]
}

@test "a top-level scalar has the empty path and no closing event" {
  run_lab_c 'printf "%s\n" "3 \"x\" [] {}" | jq -c --stream .'
  [ "$status" -eq 0 ]
  [ "$output" = $'[[],3]\n[[],"x"]\n[[],[]]\n[[],{}]' ]
}

@test "the closing event of a top-level array has a path of length one" {
  run_lab_c 'printf "%s\n" "1 2 [3]" | jq -c --stream .'
  [ "$status" -eq 0 ]
  [ "$output" = $'[[],1]\n[[],2]\n[[0],3]\n[[0]]' ]
}

@test "a duplicate object key appears twice in the stream, which a parsed document hides" {
  run_lab_c 'printf "%s\n" "{\"a\":1,\"a\":2}" | jq -c --stream .; printf "%s\n" "{\"a\":1,\"a\":2}" | jq -c .'
  [ "$status" -eq 0 ]
  [ "$output" = $'[["a"],1]\n[["a"],2]\n[["a"]]\n{"a":2}' ]
}

@test "tostream then fromstream rebuilds the input" {
  run_lab_c 'printf "%s\n" "{\"a\":[1,{\"b\":2}],\"c\":[],\"d\":{},\"e\":null}" | jq -ce ". as \$d | fromstream(\$d | tostream) == \$d"'
  [ "$status" -eq 0 ]
  [ "$output" = true ]
}

@test "--slurp with --stream yields one array holding every event" {
  run_lab_c 'printf "%s" "[1,[2]]" | jq -c -s --stream .'
  [ "$status" -eq 0 ]
  [ "$output" = '[[[0],1],[[1,0],2],[[1,0]],[[1]]]' ]
}

@test "without -n the first event is consumed as dot, so inputs sees one fewer" {
  run_lab_c 'printf "%s\n" "[1,2]" | jq -c --stream "reduce inputs as \$e (0; . + 1)"
    printf "%s\n" "[1,2]" | jq -c -n --stream "reduce inputs as \$e (0; . + 1)"'
  [ "$status" -eq 0 ]
  [ "$output" = $'2\n3' ]
}

# --- rebuilding elements: the fromstream trap ------------------------------------------------

@test "the textbook fromstream(1|truncate_stream(inputs)) silently drops scalar elements" {
  run_lab_c 'printf "%s\n" "[1,2,{\"a\":3},\"s\",[4]]" | jq -cn --stream "fromstream(1|truncate_stream(inputs))"'
  [ "$status" -eq 0 ]
  [ "$output" = $'{"a":3}\n[4]' ]
}

@test "the textbook fromstream idiom also drops empty-array and empty-object elements" {
  run_lab_c 'printf "%s\n" "[[],{},1,{\"k\":[]}]" | jq -cn --stream "fromstream(1|truncate_stream(inputs))"'
  [ "$status" -eq 0 ]
  [ "$output" = '{"k":[]}' ]
}

@test "forgetting -n with fromstream loses the first leaf of the first element without any error" {
  run_lab_c '"$1/gen-array.sh" 3 >"$2/r.json"
    jq -c --stream "fromstream(1|truncate_stream(inputs))" "$2/r.json"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'{"name":"user-0","tags":["a","b"],"score":0}\n{"id":1,"name":"user-1","tags":["a","b"],"score":1}\n{"id":2,"name":"user-2","tags":["a","b"],"score":2}' ]
}

@test "to-ndjson.sh keeps scalar elements and equals jq -c .[] on an array" {
  run_lab_c 'printf "%s\n" "[1,2,{\"a\":3},\"s\",[4]]" >"$2/m.json"
    "$1/to-ndjson.sh" "$2/m.json" >"$2/got"
    jq -c ".[]" "$2/m.json" >"$2/want"
    cmp "$2/got" "$2/want" && cat "$2/got"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'1\n2\n{"a":3}\n"s"\n[4]' ]
}

@test "to-ndjson.sh emits the member values of a top-level object" {
  run_lab_c 'printf "%s\n" "{\"a\":1,\"b\":{\"c\":2},\"d\":[3]}" >"$2/o.json"
    "$1/to-ndjson.sh" "$2/o.json"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'1\n{"c":2}\n[3]' ]
}

@test "to-ndjson.sh matches jq -c .[] on 1000 generated records" {
  run_lab_c '"$1/gen-array.sh" 1000 >"$2/a.json"
    "$1/to-ndjson.sh" "$2/a.json" >"$2/got"
    "$1/gen-array.sh" 1000 ndjson >"$2/want"
    cmp "$2/got" "$2/want" && wc -l <"$2/got" | tr -d " "' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = 1000 ]
}

@test "leaf-sum.sh sums one field of 1000 records without loading the document" {
  run_lab_c '"$1/gen-array.sh" 1000 | "$1/leaf-sum.sh" score' inline "$LAB_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = 49500 ]
}

@test "path-values.sh joins paths with dots, so two different documents print the same line" {
  run_lab_c 'printf "%s\n" "{\"a.b\":1}" | "$1/path-values.sh"
    printf "%s\n" "{\"a\":{\"b\":1}}" | "$1/path-values.sh"' inline "$LAB_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = $'a.b=1\na.b=1' ]
}

# --- memory ----------------------------------------------------------------------------------

@test "peak memory of --stream stays flat as the input grows 4x, while a parsed document scales" {
  build_peak
  run_lab_c '"$1/gen-array.sh" 50000 >"$2/s.json"; "$1/gen-array.sh" 200000 >"$2/l.json"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  peak_bytes -n --stream 'reduce (inputs | select(length == 2)) as $e (0; . + 1)' "$W/s.json"
  stream_small=$PEAK_BYTES
  peak_bytes -n --stream 'reduce (inputs | select(length == 2)) as $e (0; . + 1)' "$W/l.json"
  stream_large=$PEAK_BYTES
  peak_bytes -c '.[]' "$W/s.json"
  dom_small=$PEAK_BYTES
  peak_bytes -c '.[]' "$W/l.json"
  dom_large=$PEAK_BYTES
  # streaming: small and large inputs differ by under 4 MiB and the large run stays under 64 MiB
  [ "$stream_large" -lt $((64 * 1024 * 1024)) ]
  [ $((stream_large - stream_small)) -lt $((4 * 1024 * 1024)) ]
  # parsed document: memory scales with the input, and dwarfs the streamed run
  [ "$dom_large" -gt $((2 * dom_small)) ]
  [ "$dom_large" -gt $((8 * stream_large)) ]
}

@test "--stream does not bound memory for one huge string: the string is held whole" {
  build_peak
  run_lab_c '{ printf "{\"blob\":\""; head -c 41943040 /dev/zero | tr "\0" x; printf "\"}\n"; } >"$1/big.json"' inline "$W"
  [ "$status" -eq 0 ]
  peak_bytes -c --stream 'select(length == 2) | .[0]' "$W/big.json"
  [ "$PEAK_BYTES" -ge $((40 * 1024 * 1024)) ]
}

# --- early exit and incremental reading ------------------------------------------------------

@test "first(inputs) stops reading: the producer dies before it finishes" {
  run_lab_c '"$1/producer.sh" ndjson "$2/marker" 400000 | jq -cn "first(inputs)"
    if [ -e "$2/marker" ]; then echo producer-finished; else echo producer-cut-off; fi' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'{"a":0}\nproducer-cut-off' ]
}

@test "--stream emits events from a never-closed array, where a parsed read would wait for EOF" {
  run_lab_c '"$1/producer.sh" open-array "$2/streamed" 400000 | jq -cn --stream "first(inputs)"
    if [ -e "$2/streamed" ]; then echo producer-finished; else echo producer-cut-off; fi
    "$1/producer.sh" open-array "$2/parsed" 400000 | jq -cn "first(inputs)" 2>/dev/null
    echo "parsed_rc=$?"
    if [ -e "$2/parsed" ]; then echo producer-finished; else echo producer-cut-off; fi' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'[[0,"a"],0]\nproducer-cut-off\nparsed_rc=5\nproducer-finished' ]
}

# --- errors ----------------------------------------------------------------------------------

@test "--stream emits the events before a syntax error, then fails" {
  run_lab_c 'printf "%s" "{\"a\":1,\"b\":[1,2," | jq -c --stream . 2>"$1/err"; echo "rc=$?"; cat "$1/err"' inline "$W"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '[["a"],1]' ]
  [ "${lines[1]}" = '[["b",0],1]' ]
  [ "${lines[2]}" = '[["b",1],2]' ]
  case ${lines[3]} in
    rc=[1-9]*) ;;
    *) false ;;
  esac
  case ${lines[4]} in
    *"parse error"*"Unfinished JSON term at EOF"*) ;;
    *) false ;;
  esac
}

@test "--stream-errors turns the same failure into an event and exits 0" {
  run_lab_c 'printf "%s" "{\"a\":1,\"b\":[1,2," | jq -c --stream-errors .; echo "rc=$?"'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '[["a"],1]' ]
  [ "${lines[1]}" = '[["b",0],1]' ]
  [ "${lines[2]}" = '[["b",1],2]' ]
  case ${lines[3]} in
    '["Unfinished JSON term at EOF at line 1, column 16",["b",2]]') ;;
    *) false ;;
  esac
  [ "${lines[4]}" = rc=0 ]
}

# --- depth -----------------------------------------------------------------------------------

@test "a document nested 20000 deep is refused by the parser but accepted by --stream" {
  run_lab_c 'awk "BEGIN { for (i = 0; i < 20000; i++) printf \"[\"; printf \"1\"; for (i = 0; i < 20000; i++) printf \"]\"; print \"\" }" >"$1/deep.json"
    jq -c length "$1/deep.json" 2>&1
    echo "parse_rc=$?"
    jq -c --stream "select(length == 2) | .[0] | length" "$1/deep.json"
    echo "stream_rc=$?"' inline "$W"
  [ "$status" -eq 0 ]
  case ${lines[0]} in
    *"Exceeds depth limit for parsing"*) ;;
    *) false ;;
  esac
  [ "${lines[1]}" != parse_rc=0 ]
  [ "${lines[2]}" = 20000 ]
  [ "${lines[3]}" = stream_rc=0 ]
}

# --- --seq framing ---------------------------------------------------------------------------

@test "--seq writes RS before and LF after every output" {
  run_lab_c 'printf "\03612\n\036[3]\n" | jq -c --seq . | od -An -tx1 | tr -d " \n"'
  [ "$status" -eq 0 ]
  [ "$output" = 1e31320a1e5b335d0a ]
}

@test "--seq abandons input that has no RS, with a warning and exit status 0" {
  run_lab_c 'printf "%s\n" "12 [3]" | jq -c --seq . 2>"$1/err"; echo "rc=$?"; cat "$1/err"' inline "$W"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = rc=0 ]
  case ${lines[1]} in
    *"ignoring parse error: Unfinished abandoned text at EOF"*) ;;
    *) false ;;
  esac
}

@test "--seq resynchronizes after a truncated text and drops only that text" {
  run_lab_c 'printf "\036{\"a\":1}\n\036{\"b\":\036{\"c\":3}\n" | jq -c --seq . 2>"$1/err" | od -An -tx1 | tr -d " \n"
    echo
    grep -c "ignoring parse error: Truncated value" "$1/err"' inline "$W"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 1e7b2261223a317d0a1e7b2263223a337d0a ]
  [ "${lines[1]}" = 1 ]
}

@test "--seq drops a text cut off before a newline without any warning" {
  run_lab_c 'printf "\036{\"a\":1}\n\036{\"b\":\n\036{\"c\":3}\n" | jq -c --seq . 2>"$1/err" | od -An -tx1 | tr -d " \n"
    echo
    wc -c <"$1/err" | tr -d " "' inline "$W"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 1e7b2261223a317d0a1e7b2263223a337d0a ]
  [ "${lines[1]}" = 0 ]
}

# --- shell quoting ---------------------------------------------------------------------------

@test "select-key.sh passes the key as data, select-key-unsafe.sh lets it rewrite the filter" {
  run_lab_c 'printf "%s\n" "{\"x\":1,\"y\":[2]}" >"$2/k.json"
    key=$(printf "%s" "x\" or true or \"")
    "$1/select-key.sh" "$2/k.json" "$key"
    echo "-- unsafe"
    "$1/select-key-unsafe.sh" "$2/k.json" "$key"' inline "$LAB_DIR" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'-- unsafe\n[["x"],1]\n[["y",0],2]\n[["y",0]]\n[["y"]]' ]
}
