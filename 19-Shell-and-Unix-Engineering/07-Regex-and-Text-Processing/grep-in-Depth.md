---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://pubs.opengroup.org/onlinepubs/9799919799/utilities/grep.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap09.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap12.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/functions/exit.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/functions/fclose.html"
  - "https://ftpmirror.gnu.org/grep/grep-3.12.tar.xz"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/grep.c"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/util.c"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/file.c"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/grep.1"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/Makefile"
  - "https://raw.githubusercontent.com/freebsd/freebsd-src/main/lib/libc/regex/regcomp.c"
  - "https://raw.githubusercontent.com/apple-oss-distributions/text_cmds/main/grep/grep.c"
  - "https://raw.githubusercontent.com/apple-oss-distributions/text_cmds/main/grep/file.c"
  - "https://raw.githubusercontent.com/mirror/busybox/master/findutils/grep.c"
  - "https://raw.githubusercontent.com/mirror/busybox/master/libbb/recursive_action.c"
  - "https://raw.githubusercontent.com/mirror/busybox/master/libbb/appletlib.c"
  - "https://raw.githubusercontent.com/mirror/busybox/master/libbb/xfunc_die.c"
  - "https://raw.githubusercontent.com/mirror/busybox/master/libbb/getopt32.c"
  - "https://git.musl-libc.org/cgit/musl/plain/src/stdio/__stdio_exit.c"
  - "https://git.musl-libc.org/cgit/musl/plain/src/regex/regcomp.c"
  - "https://git.musl-libc.org/cgit/musl/plain/src/misc/getopt_long.c"
  - "https://lists.freebsd.org/pipermail/freebsd-current/2010-August/019310.html"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2012-5667"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2015-1345"
---

# grep in Depth

> [!summary] Claim
> `grep` is a buffer scanner, not a line reader: GNU grep reads 96 KiB page-aligned blocks, runs a fixed-string prefilter and a lazily built DFA over the whole block, and only then looks for the newlines around a hit, which is why its speed, its binary-file heuristic, its `-m` file offset, and its exit status all depend on buffer boundaries and implementation details that POSIX leaves open.

Part of [Regular Expressions and Text Processing](README.md) in [Shell and Unix Engineering](../README.md).

## TL;DR

- **Mechanism**: GNU grep 3.12 `read`s 96 KiB aligned chunks (`GOOD_READSIZE_MIN` in `src/grep.c`), finds candidate offsets with Boyer-Moore or Aho-Corasick (`src/kwset.c`), confirms them with a lazy DFA (`lib/dfa.c`), and falls back to a backtracking regex matcher only for back-references (`EGexecute` in `src/dfasearch.c`).
- **Rule**: In scripts, use only the POSIX options (`-E -F -c -e -f -i -l -n -q -s -v -x`), pass patterns with `-e` or `-F`, put `--` or `./` before file operands, and read the exit status as 0 selected, 1 none, greater than 1 error (POSIX.1-2024 XCU grep, EXIT STATUS).
- **Trap**: A NUL byte silently turns output off: GNU prints `grep: FILE: binary file matches` on stderr and nothing on stdout, BSD prints `Binary file FILE matches` on stdout, BusyBox prints the line, and the BSD check only looks at the first 32 KiB (measured by the lab) unless `--mmap` is given.
- **Portability fact**: `-m`, `-r`, `-R`, `\d`, `-f /dev/null`, and writing to the input file all behave differently across GNU grep 3.12, macOS grep 2.6.0-FreeBSD, and BusyBox 1.37 on musl, and the lab asserts each one (as of 2026-11).
- **When not to use it**: Do not use `grep` for matches that span lines, for structured formats (JSON, CSV with quoting), for extraction with capture groups, or for recursive code search at scale, where [[awk-Language]], `jq`, [[Perl-One-Liners]], or ripgrep ([[ripgrep-Internals]]) fit better.

## Mental model

```mermaid
flowchart TD
  A["argv: options and pattern_list<br/>(permuted by getopt_long)"] --> B["split patterns at newline<br/>drop duplicates (hash)"]
  B --> C{"matcher"}
  C -->|"-F, or -E/-G that is all literal"| K["kwset<br/>Boyer-Moore or Aho-Corasick"]
  C -->|"-G / -E"| D["dfaparse + dfamust<br/>kwset of must-have strings"]
  C -->|"-P"| P["PCRE2 + JIT<br/>one line at a time"]
  R["read() 96 KiB aligned block<br/>sentinel newline before it"] --> N{"NUL in block<br/>or file hole?"}
  N -->|"yes, default"| Q["out_quiet: suppress lines<br/>later report binary file matches"]
  N -->|"no, or -a"| S["search the whole block"]
  Q --> S
  K --> S
  D --> S
  P --> S
  S --> F["kwset finds candidate offset"]
  F --> G["superset DFA, then DFA"]
  G -->|"back-reference"| H["GNU regex re_search<br/>backtracking"]
  G -->|"accept"| L["memrchr / rawmemchr<br/>to the enclosing newlines"]
  H --> L
  L --> O["print line, or count, or exit 0 on -q"]
  O --> X["exit: 0 selected, 1 none, 2 error<br/>-q with a match wins over errors"]
```

The pattern side and the input side meet only in the search step.
On the left, `main` splits every `-e` operand and `-f` file at newlines into one pattern list, removes duplicates with a hash table (`update_patterns`), and picks a matcher; on the right, `fillbuf` reads raw blocks with `read(2)` into a page-aligned buffer and `grep()` checks each block for NUL bytes before searching it.
The search never iterates over lines: the fixed-string matcher jumps to a candidate offset, the DFA confirms it, and only a confirmed hit makes grep scan backward and forward for the newlines that bound the line it prints.
Back-references are the one feature the DFA cannot decide, so only those lines pay for the backtracking matcher, and the exit status summarizes what happened across every file operand.

## How it works

### What POSIX specifies

POSIX.1-2024 XCU grep defines a small utility.
Patterns come from `-e pattern_list`, `-f pattern_file`, or the first operand, and a `pattern_list` "shall consist of one or more patterns separated by <newline> characters", so one shell word can carry many patterns (POSIX.1-2024 XCU grep, DESCRIPTION).
The DESCRIPTION says that "a null BRE shall match every line", and "a null pattern can be specified by an empty line in pattern_file" (XCU grep, DESCRIPTION and OPTIONS).
Matching is line based: "there is no way for a pattern to match a <newline> found in the input" (XCU grep, DESCRIPTION).
The required options are `-E`, `-F`, `-c`, `-e`, `-f`, `-i`, `-l`, `-n`, `-q`, `-s`, `-v`, and `-x`; `-o`, `-w`, `-r`, `-m`, `-A`, `-B`, `-C`, `-H`, `-h`, `-L`, `-a`, `-z`, and `-P` are extensions.
The exit status is 0 when "one or more lines were selected and the output specified in STDOUT was successfully written", 1 when no lines were selected, and greater than 1 on error (XCU grep, EXIT STATUS, reworded by Austin Group Defect 1502 in Issue 8).
CONSEQUENCES OF ERRORS adds the rule that makes `-q` special: "If the -q option is specified, the exit status shall be zero if an input line is selected, even if an error was detected."
The input files "shall be text files", and APPLICATION USAGE recommends `LC_ALL=C` when processing pathnames because some byte sequences are not valid characters in other locales.
XBD 9.2 says what a NUL means to a regex utility: if a NUL appears in the text, "that NUL may designate the end of the text string" (POSIX.1-2024 XBD 9.2), which is exactly what BusyBox's `puts`-based output does.
XBD 9.3.2 makes a backslash before an ordinary character undefined in a BRE, except for `\(`, `\)`, `\{`, `\}`, `\1` to `\9`, `\]`, and the implementation-defined `\?`, `\+`, and `\|` (POSIX.1-2024 XBD 9.3.2), so `\d`, `\w`, and `\s` are all outside the standard; [[BRE-ERE-and-PCRE]] covers the flavors themselves.

### GNU grep 3.12: options, patterns, and the matcher choice

`main` in `src/grep.c` calls `setlocale (LC_ALL, "")` and `init_localeinfo` before parsing, so the locale is fixed for the whole run.
Options are parsed by `getopt_long`, which permutes `argv` so an option after a file name is still an option unless `POSIXLY_CORRECT` is set (grep-3.12 `doc/grep.texi`, Environment Variables).
`-E`, `-F`, `-G`, and `-P` each call `setmatcher`, which dies with `conflicting matchers specified` if two differ; the `matchers[]` table maps them to `GEAcompile`/`EGexecute`, `Fcompile`/`Fexecute`, and `Pcompile`/`Pexecute`.
Every `-e` argument and every `-f` file is appended to one newline-terminated buffer and passed through `update_patterns`, which hashes each pattern (`hash_pattern`, a djb2 variant) and drops exact duplicates while keeping a `patloc` table so error messages can still name the file and line a pattern came from.
If `-f` contributed zero patterns (`grep -f /dev/null`), `main` inverts the match and then returns `EXIT_FAILURE` before opening any input: "If it is easy to see that matching cannot succeed (e.g., 'grep -f /dev/null'), fail without reading the input."
That early return is why `grep -c -f /dev/null file` prints nothing at all in GNU grep, while BSD grep and BusyBox print `0`; the lab asserts both.
A pattern operand (not an `-e` argument) that starts with `\-` has the backslash skipped unless `-F` is in effect (`skip_bs` in `main`), so the common non-POSIX idiom `grep '\-x'` does not trigger a stray-backslash warning.
The matcher can change after parsing: in a single-byte locale a single `-F -w` pattern is rewritten into a BRE (`fgrep_to_grep_pattern`) because the DFA handles words faster, and two or more `-E` or `-G` patterns that are all literal are rewritten into `-F` form (`try_fgrep_pattern`) because Aho-Corasick beats a DFA there.

### GNU grep: the buffer

`main` computes `good_readsize = ALIGN_TO (GOOD_READSIZE_MIN, pagesize)` with `GOOD_READSIZE_MIN = 96 * 1024`; the comment records that 96 KiB "gave good results in a benchmark in 2018" and that a 2024 rerun "found no significant difference for values from 32 KiB to 1024 KiB" (grep-3.12 `src/grep.c`).
`reset` places `bufbeg` on a page boundary and writes the end-of-line byte at `bufbeg[-1]`, a sentinel that lets backward scans for a line start stop without a bounds check.
For standard input it records `lseek (fd, 0, SEEK_CUR)`, so grep knows whether its input is seekable and where it started.
`fillbuf` calls `safe_read` for `good_readsize` bytes, keeps the unfinished last line (`save`) by moving it to just before the new data, and grows the buffer with `xpalloc` capped near the size `fstat` reported, so a huge file does not force a huge allocation up front.
After each read it zeroes one machine word past `buflim` so the word-at-a-time scanners can read past the end harmlessly.
Under `-z`, or once a NUL has marked the file as binary, and only when the pattern cannot match an empty line, `fillbuf` skips each all-zero block with `lseek (fd, offset, SEEK_DATA)`, so a sparse file is not read hole by hole; the manual notes the optimization is lost with `-a` unless `-z` is also given (grep-3.12 `doc/grep.texi`, Performance).
Opening uses `openat` with `O_RDONLY | O_NOCTTY`, adds `O_NOFOLLOW` when the file must not be followed and `O_NONBLOCK` when devices are being skipped (`grepfile`), so a recursive search cannot block forever opening a FIFO.

### GNU grep: binary files

At the top of every block, `grep()` asks `buf_has_nulls`, which stores a 0 at the end of the block and compares `strlen` with the block size; on the first block it also asks `file_must_have_nulls`, which uses `lseek (fd, cur, SEEK_HOLE)` because a hole before end of file must read back as zeros.
On the first NUL, with the default `--binary-files=binary`, grep sets `out_quiet` (unless `-c` is given) so later matching lines are counted but not printed, sets `done_on_match` (unless `-m` is given) so it stops at the next match, and replaces NULs with newlines (`zap_nuls`) so a megabyte of zeros does not become one giant line.
At the end, if a match was found after the NUL was seen, `grep()` calls `error (0, 0, _("%s: binary file matches"), input_filename ())`, which goes to stderr.
That location changed in grep 3.5 (2020-09-27): the old `Binary file FOO matches` line went to stdout, so "commands like 'grep PATTERN FILE | wc' no longer add 1 to the count" (grep-3.12 `NEWS`).
Because the check happens per block, a matching line before the first NUL-bearing block is printed normally, and only matches after it are suppressed; the manual warns that the outcome depends "on hardware and operating system characteristics such as system page size and input buffering" (grep-3.12 `doc/grep.texi`, File and Directory Selection).
Encoding errors are treated separately: in a multibyte locale a line that would be output and fails `buf_has_encoding_errors` is suppressed and also triggers the message, while `skip_easy_bytes` skips ASCII-only words using a per-locale `unibyte_mask` (0x80 repeated for UTF-8).
`-I` (`--binary-files=without-match`) returns from `grep()` as soon as a NUL is found, and `-a` disables the check entirely.

### GNU grep: the search

`grepbuf` calls the matcher's `execute` on the whole region from the current position to the last complete line, not on one line.
For `-G` and `-E`, `GEAcompile` builds three things: a DFA with `dfaparse` and `dfacomp`, a `kwset` of strings that every match must contain (`kwsmusts`, from `dfamust`), and, only when a pattern may contain `\1` to `\9` (`possible_backrefs_in_pattern`) or uses a construct the DFA does not support, compiled regex objects.
`EGexecute` runs `kwsexec` to find the next must-have string anywhere in the block, uses `memrchr` to back up to that line's start, runs the superset DFA and then the exact DFA on just that region, and only if `dfaexec` reports `backref` does it call `re_search`.
`kwsprep` uses Boyer-Moore for one string and Aho-Corasick for several (`src/kwset.c`); the Boyer-Moore half is what Mike Haertel, the original author, described in 2010: grep "AVOIDS LOOKING AT EVERY INPUT BYTE", "EXECUTES VERY FEW INSTRUCTIONS FOR EACH BYTE", and "AVOIDS BREAKING THE INPUT INTO LINES".
The DFA is built lazily: states and transition tables are created on first use, and `MAX_TRCOUNT = 1024` caps the number of transition tables alive at once, after which `build_state` frees them and rebuilds the hot ones (grep-3.12 `lib/dfa.c`), so memory stays bounded even for patterns whose full DFA would be exponential.
`dfasupported` returns false for back-references, for multibyte bracket sets (`MBCSET`), and for `\<`, `\>`, `\b`, and `\B` in a multibyte locale, which is why the same pattern can be much slower under `LANG=en_US.UTF-8` than under `LC_ALL=C`; the manual says so directly: "Setting LC_ALL='C' can be particularly efficient, as grep is tuned for that locale" (grep-3.12 `doc/grep.texi`, Performance).
`-w` is implemented by wrapping the pattern in `\(^\|[^[:alnum:]_]\)\(` and `\)\([^[:alnum:]_]\|$\)` for the DFA, then re-checking each candidate with `re_search` and `re_match`, retrying shorter matches at the same start and later starts until the word boundaries line up (`EGexecute`).
`-x` wraps the pattern in `^\(` and `\)$` the same way.
`-P` is different in kind: `Pcompile` uses PCRE2 with `PCRE2_UTF` and `PCRE2_MATCH_INVALID_UTF` in UTF-8 locales, refuses other multibyte locales, accepts exactly one pattern ("the -P option only supports a single pattern"), JIT-compiles it, and `Pexecute` searches "line by line" because the multiline mode was "not always better and the correctness issues were too puzzling" (grep-3.12 `src/pcresearch.c`).
The JIT stack starts at 32 KiB and doubles on `PCRE2_ERROR_JIT_STACKLIMIT`; a backtracking blow-up ends the whole run with `exceeded PCRE's backtracking limit` and status 2, naming the file since grep 3.6, which is the ReDoS failure mode [[ReDoS]] covers.

### GNU grep: output, early exit, and the file offset

`main` `fstat`s standard output before searching.
If it is a regular file, its device and inode go into `out_stat`, and `grepdesc` later refuses any input with the same inode: `grep: FILE: input file is also the output`, status 2, because appending matches to the file being read could loop until the disk fills.
If it is `/dev/null` (`SAME_INODE` against `stat ("/dev/null")`), grep sets `dev_null_output` and treats the run like `-q` for matching, stopping each file at its first match.
`-q` sets `exit_on_match`, and `grepbuf` calls `exit (EXIT_SUCCESS)` at the first selected line, which implements the POSIX rule that a match beats earlier errors.
The two quiet modes differ at the end of input: after a `>/dev/null` run stops early, `finalize_input` drains a pipe on standard input (with `splice` on Linux), so the writer never sees `SIGPIPE`, while `-q` exits at once and leaves the writer to get `EPIPE` (see [[SIGPIPE-and-EPIPE]]).
With `-m NUM` on standard input, `prtext` records `after_last_match`, and `finalize_input` calls `lseek (fd, after_last_match, SEEK_SET)`, so the next process sharing that open file description starts reading right after the last selected line; the manual documents the `while grep -m 1 PATTERN; do ...; done < FILE` idiom and warns that it "probably will not work" through a pipe (grep-3.12 `doc/grep.texi`, General Output Control).
`main` returns `errseen ? EXIT_TROUBLE : status`, so any suppressible error (missing file, unreadable directory) yields 2 even when other files matched, and `-s` hides the message but not the status.

### BSD grep (FreeBSD and macOS)

macOS ships the FreeBSD grep (`grep -V` prints `grep (BSD grep, GNU compatible) 2.6.0-FreeBSD` on this host) with Apple changes in `text_cmds`.
It is line oriented: `grep_fgetln` in `file.c` `read`s `MAXBUFSIZ` (32 KiB) chunks, copies each line into `lnbuf`, and `procline` in `util.c` runs `regexec` once per pattern per line with `REG_STARTEND`.
Binary detection is a single `memchr (bufpos, '\0', bufrem)` over the first chunk in `grep_open`, so a NUL after byte 32768 is never seen; the lab measures this boundary.
The exception is `--mmap` (`FILE_MMAP`), which maps the whole regular file so the same `memchr` covers all of it: on this host `grep --mmap match far.bin` reports `Binary file far.bin matches` where the default read path prints the line (measured 2026-11-14).
When a binary file matches, `procfile` prints `Binary file %s matches` with `printf`, which goes to stdout.
`-m` stops the loop, but nothing seeks standard input back, so the rest of the 32 KiB chunk is lost to the next reader.
Apple's `grep.c` adds `REG_ENHANCED` to the `regcomp` flags for both BRE and ERE, which is why `\d`, `\+`, and similar escapes work on macOS and nowhere in POSIX.
Recursion uses `fts_open`; on FreeBSD the default `linkbehave` is `LINK_SKIP`, and Apple sets `LINK_SKIP` whenever `-r` or `-R` is given (`rdar://problem/25930963`), so both skip every symlink, including one named on the command line, unless `-S` (follow all) or `-O` (follow command-line links) is added.
BSD grep still reads `GREP_OPTIONS` and prepends it to `argv` (`grep.c`, "support for extra arguments in GREP_OPTIONS"), where GNU grep ignores it since 3.6.
The final status is `matched ? (file_err ? (qflag ? 0 : 2) : 0) : (file_err ? 2 : 1)`, the same contract as GNU; FreeBSD additionally inverts `matched` for `-L`, while Apple removed that inversion (`rdar://problem/88986027`).

### BusyBox grep

BusyBox grep (`findutils/grep.c`) describes itself as a "Mini grep implementation for busybox using libc regex" and its audit note says "always acts as -a": `-a` and `-I` are parsed and ignored.
It reads one line at a time with `xmalloc_fgetline` (or `getdelim` when `CONFIG_EXTRA_COMPAT` adds `-z`), runs `strstr` or `strcasestr` for `-F` and `regexec` for everything else, and prints with `puts`, so a NUL inside a printed line truncates it.
The regex dialect is the C library's: on Alpine that is musl, whose `regcomp.c` expands `\d`, `\s`, and `\w` through `tre_macros` and accepts `\+`, `\?`, and `\|` in BREs as extensions.
`-q` calls `exit_SUCCESS ()` at the first match, `-m` breaks the read loop right after the last allowed match, and the applet ends through `exit`, so musl's `__stdio_exit` seeks standard input back to the stream position (`if (f->rpos != f->rend) f->seek(f, f->rpos-f->rend, SEEK_CUR)`), which POSIX requires of `exit` for a seekable input stream (POSIX.1-2024 XSH exit); the observable effect matches GNU's explicit `lseek`.
`-r` uses `recursive_action` with `ACTION_FOLLOWLINKS_L0`, so a command-line symlink is followed; inside the tree `lstat` is used, but `file_action_grep` skips only symlinks to directories, so symlinks to files are read.
The exit status is 2 if any file failed to open, else `!matched`.

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| GNU read size | 96 KiB (`GOOD_READSIZE_MIN = 96 * 1024`), rounded up to the page size | grep-3.12 `src/grep.c` `main`, `fillbuf` |
| GNU binary check window | every block read (whole file), plus `SEEK_HOLE` once | grep-3.12 `src/grep.c` `grep`, `file_must_have_nulls` |
| BSD read size and binary window | 32 KiB (`MAXBUFSIZ = 32 * 1024`), NUL checked in the first read only (the whole file with `--mmap`) | FreeBSD and Apple `grep/file.c` |
| BSD binary boundary | NUL at byte 32706 detected, at 32806 missed | measured on this host (macOS 27.3, grep 2.6.0-FreeBSD), 2026-11-14 |
| Lab NUL offsets | 40006 (inside GNU's first read, outside BSD's) and 200006 (outside both) | lab, `binary` mode |
| GNU DFA transition tables kept | 1024 (`MAX_TRCOUNT`), then flushed and rebuilt | grep-3.12 `lib/dfa.c` |
| GNU `-P` JIT stack | 32 KiB initial, doubled on `PCRE2_ERROR_JIT_STACKLIMIT` | grep-3.12 `src/pcresearch.c` `jit_exec` |
| GNU `-P` patterns | exactly 1 | grep-3.12 `src/pcresearch.c` `Pcompile` |
| Boyer-Moore cost | fewer than 3 x86 instructions per examined byte (2010) | Haertel, freebsd-current, 2010-08-21 |
| Exit status | 0 selected, 1 none, greater than 1 error (GNU, BSD, BusyBox use 2) | POSIX.1-2024 XCU grep; lab `exit` mode |
| `grep -c` with zero matches | prints `0`, exits 1 | lab `exit` mode |
| egrep and fgrep | deprecated since 2.5.3 (2007), warn since 3.8 (2022-09-02) | grep-3.12 `NEWS` |
| `GREP_OPTIONS` | obsolescent in 2.21 (2014), ignored since 3.6 (2020-11-08) | grep-3.12 `NEWS` |
| Long-line overflow | CVE-2012-5667: integer overflows before 2.11 | NVD |
| `-F` heap over-read | CVE-2015-1345: `bmexec_trans` in 2.19 to 2.21, fixed in 2.22 | NVD; grep-3.12 `NEWS` |

## Portability

The shell under test does not change which `grep` runs, so the lab runs under every shell in the matrix but branches on the grep implementation it detects; no shell has a `grep` builtin.
The shell matters only for what it passes in: native zsh does not field split an unquoted `$opts`, and a bare `*` can expand to a file named `-v` in every shell.

| Behavior | POSIX.1-2024 | GNU grep 3.12 | macOS grep 2.6.0-FreeBSD | FreeBSD main | BusyBox 1.37 (musl) |
|---|---|---|---|---|---|
| Exit 0/1/2; `-q` + error + match gives 0; `-s` keeps 2 | required | yes (lab) | yes (lab) | same expression in `grep.c` | yes (lab) |
| Newline in `-e` splits patterns; empty pattern matches all | required | yes (lab) | yes (lab) | `strsep` on newline | yes (lab) |
| `grep -c -f /dev/null` output | not specified (the FreeBSD man page calls it undefined) | nothing, exit 1 (lab) | `0`, exit 1 (lab) | `0` expected from the shared `procfile` code | `0`, exit 1 (lab) |
| NUL in input | input "shall be text files" | stderr `binary file matches`, every block checked (lab) | stdout `Binary file F matches`, first 32 KiB only (lab), whole file with `--mmap` (measured) | same as macOS (`file.c`) | prints the line, no detection (lab) |
| `-m 1` on seekable stdin, offset after | not specified | after the selected line (lab) | end of the 32 KiB read (lab) | same as macOS (no `lseek`) | after the selected line via musl `exit` (lab) |
| `-r` and a symlink to a file inside the tree | not specified | skipped (lab) | skipped (lab) | skipped (`LINK_SKIP`) | read (lab) |
| `-r` on a command-line symlink to a directory | not specified | followed (lab) | not followed (lab), exit 1 (measured) | not followed (`LINK_SKIP`) | followed (lab) |
| `-R` and a symlink to a directory | not specified | followed (lab) | not followed (lab) | not followed unless `-S` | followed (lab) |
| `grep x f >>f` | not specified | refused, exit 2 (lab) | copies, exit 0 (lab) | copies (no inode check in source) | copies, exit 0 (lab) |
| Option after an operand (`grep pat f -v`) | Guideline 9: options precede operands | permuted unless `POSIXLY_CORRECT` | permuted unless `POSIXLY_CORRECT` (measured) | `getopt_long` | permuted (musl `getopt_long`) |
| `\d` in a BRE | undefined (XBD 9.3.2) | literal `d` plus `stray \ before d` warning (lab) | digit class via `REG_ENHANCED` (lab) | not a digit class: grep links `libregex`, whose GNU extensions cover `\w`, `\W`, `\s`, `\S`, `\b`, and `\B` but not `\d`, and `may_escape` rejects an escaped letter, so `regcomp` fails with `REG_EESCAPE` (`lib/libc/regex/regcomp.c`, read not run) | digit class via `tre_macros` (lab) |
| `GREP_OPTIONS` | not specified | ignored since 3.6 | honored (measured) | honored (`grep.c`) | not read (no reference in `grep.c`) |
| `-P` | not specified | PCRE2, one pattern | absent (Apple `optstr`) | absent (`optstr`) | absent (`OPTSTR_GREP`) |
| `-z` | not specified | yes | yes | yes | only with `CONFIG_EXTRA_COMPAT` |

A "(lab)" cell is a lab assertion; as of 2026-11 the lab has run only on the macOS host, so the GNU and BusyBox "(lab)" cells rest on the cited source code until the shell matrix runs them.

## Worked example

The [lab](../labs/07-grep-in-depth/) is one probe script, `grep-probe.sh MODE DIR`, and a bats file that asserts its output for GNU, BSD, and BusyBox grep.
This simplified excerpt shows the two modes that expose buffering, `binary` and `maxcount`:

```sh
# Simplified excerpt of grep-probe.sh (the lab file also defines join and flavor).
LC_ALL=C
export LC_ALL

# binary: a NUL 40006 bytes in (inside GNU's 96 KiB first read, outside BSD's 32 KiB)
# and 200006 bytes in (outside both); line 1 is the only match.
mk() {
  {
    printf 'match\n'
    awk -v n="$1" 'BEGIN {
      line = sprintf("%099d", 0); gsub(/0/, "y", line)
      for (t = 0; t + 100 <= n; t += 100) print line
    }'
    printf '\000\n'
  } >"$2"
}
printf 'match\n\000\n' >small.bin
mk 40000 near.bin
mk 200000 far.bin
for f in small.bin near.bin far.bin; do
  grep match "$f" >o.txt 2>e.txt
  rc=$?
  echo "$f: out=<$(head -n 1 o.txt)> err=<$(head -n 1 e.txt)> rc=$rc"
done

# maxcount: two readers share one open file description on stdin.
printf 'a\nx1\nb\nx2\nc\n' >m.txt
{
  first=$(grep -m 1 x)
  rest=$(cat | tr '\n' ,)
} <m.txt
echo "grep=<$first> rest=<$rest>"
```

Captured on the macOS host (`/bin/sh`, BSD grep):

```text
small.bin: out=<Binary file small.bin matches> err=<> rc=0
near.bin: out=<match> err=<> rc=0
far.bin: out=<match> err=<> rc=0
grep=<x1> rest=<>
```

What the bats file asserts for GNU grep 3.12 (from `src/grep.c`; not run on this host):

```text
small.bin: out=<> err=<grep: small.bin: binary file matches> rc=0
near.bin: out=<> err=<grep: near.bin: binary file matches> rc=0
far.bin: out=<match> err=<> rc=0
grep=<x1> rest=<b,x2,c,>
```

What it asserts for BusyBox 1.37 on musl (from `findutils/grep.c` and musl `__stdio_exit.c`; not run on this host):

```text
small.bin: out=<match> err=<> rc=0
near.bin: out=<match> err=<> rc=0
far.bin: out=<match> err=<> rc=0
grep=<x1> rest=<b,x2,c,>
```

Read the three blocks together.
The same file is "text" or "binary" depending on which implementation reads it and how far in the NUL sits, and the exit status is 0 in every case, so a pipeline that depends on the printed line silently gets nothing under GNU grep.
The `-m` case shows that grep's buffer is shared state: GNU puts back what it over-read, musl's `exit` does the same for BusyBox, and BSD grep keeps the 32 KiB it read, so the second reader starts at end of file.
The other modes assert the rest of the Portability table: `exit`, `patterns`, `recurse`, `selfout`, `dashfile`, and `escape`.

## Pitfalls

> [!warning] `grep -c` and `grep` exit 1 on "no match", which `set -e` treats as failure
> Trigger: `n=$(grep -c ERROR "$log")` in a script with `set -e`, on a log with no errors.
> Symptom: the script exits silently right after printing nothing, although `grep -c` wrote `0` (the lab's `count_zero=<0> status=1`).
> Fix: `n=$(grep -c ERROR "$log") || [ "$?" -eq 1 ]`, or test the status explicitly with `case`; never treat 1 as an error, and treat greater than 1 as one (see [[errexit-Semantics-and-Exceptions]]).

> [!warning] `grep -q` at the end of a pipeline kills the writer under `pipefail`
> Trigger: `set -o pipefail; producer | grep -q READY`.
> Symptom: the pipeline fails intermittently with 141 (128 + `SIGPIPE`) when `producer` writes after grep has exited; the GNU manual's own example is `sed '/^...$/d' | grep -q X`, which "can cause sed to exit with a nonzero status" (grep-3.12 `doc/grep.texi`, Performance).
> Fix: keep `-q` only when the producer's status does not matter, or use `grep READY >/dev/null`, which in GNU grep stops matching early but drains the pipe (`finalize_input`, `drain_input`); see [[pipefail-and-PIPESTATUS]] and [[SIGPIPE-and-EPIPE]].

> [!warning] A NUL byte or an encoding error silently suppresses output
> Trigger: searching a log that contains one NUL (a truncated write, a core dump fragment) or Latin-1 bytes in a UTF-8 locale.
> Symptom: GNU grep exits 0, prints nothing on stdout, and writes `grep: FILE: binary file matches` to stderr, which a `2>/dev/null` hides; scripts written before grep 3.5 that counted the stdout message with `wc -l` changed results when it moved to stderr (grep-3.12 `NEWS`, 3.5).
> Fix: `grep -a` (or `--binary-files=text`) when the data is text with stray bytes, `LC_ALL=C` when the issue is encoding, and never feed `-a` output to a terminal (see [[Terminal-Escape-Injection]]).

> [!warning] A blank line in a pattern file matches every line
> Trigger: `grep -v -f denylist.txt` where `denylist.txt` ends with an empty line, or `grep -e "$patterns"` where the variable holds two newlines in a row.
> Symptom: `-v` removes everything and the result is empty; without `-v` everything matches; the lab's `file_with_blank_line=3` shows a 3-line file fully selected by a list whose only real patterns never occur.
> Fix: strip blank lines (`grep -v '^$' list >clean`), use `-F -x` for exact lists, and assert the list is non-empty before using it; note that `-f /dev/null` matches nothing, which is the opposite edge.

> [!warning] File names that start with `-` become options
> Trigger: `grep secret *` in a directory where someone created a file named `-v` (or `-r`, or `--include=x`).
> Symptom: the lab's `star=<> status=1`: the glob expands to `-v data`, `getopt_long` permutes `-v` into an option, and the real match disappears with a "no match" status.
> Fix: `grep -e "$pat" -- *` or `grep -e "$pat" ./*`, as the GNU manual recommends (grep-3.12 `doc/grep.texi`, Usage); `--` alone still treats a file named `-` as standard input (POSIX.1-2024 XBD 12.2, Guideline 10 for `--` and Guideline 13 for `-`; see [[Pathname-Expansion]]).

> [!warning] grep 3.8 warnings on stderr break strict wrappers
> Trigger: `egrep`, `fgrep`, or a pattern with a stray backslash such as `\d`, `\/`, or `\:` on GNU grep 3.8 or later.
> Symptom: `egrep: warning: egrep is obsolescent` or `grep: warning: stray \ before d` on stderr; CI jobs that fail on any stderr output break, and `\d` silently means the letter `d` (the lab's `backslash_d=<ad,>`); the NEWS says the warnings "are likely to be errors in future releases" (grep-3.12 `NEWS`, 3.8).
> Fix: use `grep -E` and `grep -F`, write `[[:digit:]]` or `[0-9]`, and lint patterns for backslashes before ordinary characters.

> [!warning] `-r` returns different file sets on different systems
> Trigger: `grep -r pattern dir` where `dir` contains symlinks, or where `dir` is itself a symlink.
> Symptom: the lab's `recurse` mode: GNU skips links inside the tree but follows a command-line link, macOS skips every link and returns status 1 for `grep -r hit tlink`, and BusyBox reads file links; a security audit run on a Mac can miss what the same command finds on Linux.
> Fix: drive recursion with `find dir -type f -exec grep -H -e "$pat" {} +` so the traversal policy is explicit, or use `-R` on GNU and `-S` on BSD when following is intended.

> [!warning] `grep pat f >>f` loops or corrupts outside GNU grep
> Trigger: redirecting output into a file that is also an input, often through a glob such as `grep TODO * >todo.txt`.
> Symptom: GNU grep refuses that one file with `input file is also the output` and exits 2 (the lab), while BSD and BusyBox read their own output; for a small file the copy is harmless (`lines=2`), but once the output exceeds the stdio buffer the reader can keep finding new matches it just wrote until the disk fills, which is the loop GNU's comment in `grepdesc` describes.
> Fix: write to a temporary file in another directory and rename it.

> [!warning] Long lines and `-F` have had memory-safety bugs
> Trigger: untrusted input with lines over 2 GiB, or crafted input with `-F`.
> Symptom: CVE-2012-5667, integer overflows in GNU grep before 2.11 "via vectors involving a long input line that triggers a heap-based buffer overflow"; CVE-2015-1345, an out-of-bounds heap read in `bmexec_trans` in `kwset.c` in grep 2.19 to 2.21 with `-F` (NVD).
> Fix: keep grep current (both are fixed in every grep from 2.22), and bound input size and run time when grep processes attacker-controlled data.

## Security and robustness

- **Untrusted patterns**: pass them as `-e "$pat"` (never as the first operand, which may start with `-`) and add `-F` unless regex semantics are the point; a user-supplied BRE with a back-reference, or any `-P` pattern, can backtrack exponentially, and GNU `-P` stops with status 2 and `exceeded PCRE's backtracking limit` mid-run, so wrap the call in `timeout` and treat 2 as "unknown", not "absent" (see [[ReDoS]]).
- **Newlines inside a pattern variable**: a value with an embedded newline becomes two patterns, and an empty line becomes "match everything" (POSIX.1-2024 XCU grep, OPTIONS `-e`); validate with `case $pat in *"$nl"*) reject ;; esac` before use.
- **Exit status as a security decision**: `if grep -q "^$user:" allowlist` treats a missing or unreadable allowlist as "not found" (status 2 fails the `if` too), which is safe here but inverted for deny lists; branch on 0, 1, and greater than 1 separately.
- **`-q` hides read errors**: by POSIX rule a match in any file makes `-q` return 0 even if another file could not be read, so do not use `-q` to prove that every file was scanned.
- **File names in output**: a file name containing a newline splits one record into two; use `-l --null` (GNU and BSD; GNU also spells it `-Z`, which on macOS means decompress) with `xargs -0`, and note that POSIX.1-2024 XCU grep FUTURE DIRECTIONS encourages implementations to treat such names as an error.
- **Terminal output**: `-a` and filenames from untrusted trees can carry escape sequences; GNU grep 3.8 ignores a `GREP_COLOR` value that "attempts to inject ANSI terminal escapes" (grep-3.12 `NEWS`), but matched data itself is printed raw (see [[Terminal-Escape-Injection]]).
- **Devices and FIFOs in a tree**: since grep 2.12, GNU `-r` reads a device only if it is named on the command line, and `grepfile` opens with `O_NONBLOCK` when skipping devices, so a FIFO planted in a scanned directory cannot hang the scan; `-R` and `-D read` reopen that risk.
- **Environment**: BSD grep prepends `GREP_OPTIONS` to every command line, so a polluted environment can add `-v` or `-l` to a check on macOS (measured on this host); clear it with `env -u GREP_OPTIONS` in security-relevant wrappers.
- **Locale**: `LC_ALL=C` gives byte semantics, avoids encoding-error suppression, and avoids undefined behavior on non-UTF-8 pathnames, as POSIX APPLICATION USAGE recommends; it also changes `-i` and `[a-z]` (see [[Locale-and-LC_ALL]]).

## The principal's view

The decision is not "which grep flags" but "which contract does this call site rely on".
A call that is a predicate (`if grep -q`) relies on the exit status, so the review question is whether status 2 is handled and whether `pipefail` turns early exit into a failure.
A call that extracts data relies on the printed bytes, so the review question is what happens on a NUL, an encoding error, or a line longer than memory, because all three degrade silently.
For portable scripts, the defensible policy is the POSIX option set plus `-e`, `-F`, `--`, and `LC_ALL=C`, with anything else (`-o`, `-r`, `-m`, `-w`, `-P`, `-z`) treated as an implementation feature that must be tested on each target; the lab shows that `-m`, `-r`, and binary handling already differ among the three systems a typical team ships to.
The trade-off is expressiveness: banning `-P` and `-o` pushes extraction into `sed` or `awk`, which costs readability but buys one well-specified engine instead of three.
Change tools when the problem changes shape: use awk when fields matter ([[awk-Language]]), `sed` when a match drives an edit ([[sed-Pattern-and-Hold-Space]]), Perl or `pcre2grep -M` when matches span lines ([[Perl-One-Liners]]), ripgrep when the job is "search a repository" and parallel traversal plus ignore files matter more than POSIX ([[ripgrep-Internals]]), and a real parser or `jq` when the data is structured.
For hot paths, measure before tuning: `LC_ALL=C` and `-F` are the two cheapest wins according to the GNU manual, and back-references are the documented worst case.

## Interview questions

> [!question] L5: What exit statuses can `grep` return, and what does `grep -q pat missing.txt good.txt` return when `good.txt` matches?
>
> > [!success]- Answer
> > POSIX defines 0 for at least one selected line, 1 for none, and greater than 1 for an error; GNU, BSD, and BusyBox use 2.
> > Without `-q`, a missing file makes the status 2 even if another file matched.
> > With `-q`, POSIX CONSEQUENCES OF ERRORS requires 0 if any line is selected, even after an error, so the answer is 0 (the lab asserts `q_missing_then_match=0`).
> > `-s` only hides the message; the status stays 2.
> >
> > Follow-up: Why is `grep -q` a poor way to prove that every file in a list was readable?

> [!question] L5: Predict the output on GNU grep 3.12 of `printf 'a\nx1\nb\nx2\nc\n' >f; { grep -m 1 x; cat; } <f`, and then on macOS.
>
> > [!success]- Answer
> > GNU prints `x1`, then `cat` prints `b`, `x2`, and `c`, because `finalize_input` seeks standard input back to just after the last selected line.
> > macOS prints only `x1`: BSD grep read the whole file in its first 32 KiB `read` and never seeks back, so `cat` starts at end of file.
> > BusyBox on musl behaves like GNU, because musl's `exit` repositions a seekable input stream as POSIX requires.
> > Through a pipe instead of `<f`, every implementation loses the over-read data, as the GNU manual warns.
> >
> > Follow-up: Which open file description do grep and cat share here, and what would change with `cat f | { grep -m 1 x; cat; }`?

> [!question] L5: Why does `grep -v -f deny.txt input` sometimes print nothing at all?
>
> > [!success]- Answer
> > Each line of a pattern file is a pattern, and POSIX says an empty line is a null pattern that matches every line.
> > A trailing blank line in `deny.txt` therefore matches every input line, and `-v` removes all of them.
> > The fix is to strip blank lines from the list and to use `-F -x` when the entries are exact strings.
> >
> > Follow-up: What does GNU grep do with `-f /dev/null`, and why does `grep -c -f /dev/null` print nothing in GNU but `0` in BSD and BusyBox?

> [!question] L6: Debug this nightly job, which reports "no errors" on some nights although the log has errors: `set -eu; if grep ERROR /var/log/app.log | grep -v DEBUG >/tmp/errs; then mail_errors; else echo "no errors"; fi`.
>
> > [!success]- Answer
> > First suspect binary detection: if the log contains one NUL byte (a crash mid-write), GNU grep suppresses the matching lines, prints `binary file matches` to stderr, and the second grep sees no input, so the `if` takes the else branch.
> > Second, without `pipefail` the `if` sees only the last grep's status, so a status 2 from the first grep (log rotated away, permission change) is indistinguishable from "no errors".
> > Third, `grep -v DEBUG` exits 1 when every error line also contains `DEBUG`, which is the right answer but reached for the wrong reason.
> > The fix is `set -o pipefail` where available, `grep -a` (or `LC_ALL=C grep -a`), explicit handling of status 2, and checking that the log exists before searching it.
> >
> > Follow-up: On macOS the same NUL makes BSD grep print `Binary file /var/log/app.log matches` on stdout; what does the job mail then, and why?

> [!question] L6: Explain why GNU grep can be faster than a loop that calls `regexec` on every line, and name two features that disable the fast path.
>
> > [!success]- Answer
> > GNU grep reads 96 KiB blocks and searches the whole block, so it never splits the input into lines unless it has a candidate.
> > A Boyer-Moore or Aho-Corasick `kwset` skips over most bytes looking for a string every match must contain, and a lazily built DFA confirms the candidate with a table lookup per byte.
> > Only then does it scan for the enclosing newlines, which is Haertel's "avoids breaking the input into lines".
> > Back-references force a fallback to the backtracking `re_search`, and in a multibyte locale word assertions and multibyte bracket sets make `dfasupported` return false, so `LC_ALL=C` often restores speed.
> > `-P` uses PCRE2 one line at a time and has none of this machinery.
> >
> > Follow-up: What bounds the DFA's memory when the full automaton would be exponential in the pattern size?

> [!question] L6: A security scan runs `grep -r -l 'AKIAIOSFODNN7EXAMPLE' /srv` on Linux and on a Mac build box, and the Mac finds fewer files; why?
>
> > [!success]- Answer
> > The recursion policies differ.
> > GNU `-r` follows symlinks named on the command line and skips symlinks met during the walk; `-R` follows all of them.
> > macOS grep sets `LINK_SKIP` for both `-r` and `-R`, so it skips every symlink, including `/srv` itself if `/srv` is a symlink, and then exits 1.
> > BusyBox `-r` follows command-line links and reads symlinks to files inside the tree.
> > The robust fix is to make traversal explicit with `find -L` or `find` plus `-exec grep -l -e PAT {} +`, so the policy is in the script, not in the grep build.
> >
> > Follow-up: Which BSD option restores GNU's `-r` behavior, and which restores `-R`?

> [!question] L7+: Design a portable "does this log contain a fatal line" check for a fleet of Alpine, Debian, and macOS hosts, and justify each choice.
>
> > [!success]- Answer
> > Use `LC_ALL=C grep -a -E -e "$re" -- "$file" >/dev/null` and branch on status 0, 1, and greater than 1 separately.
> > `LC_ALL=C` removes locale-dependent encoding-error suppression and gives byte semantics on all three.
> > `-a` makes GNU and BSD treat NULs as text, and BusyBox already does; `-a` is an extension, but all three accept it.
> > `>/dev/null` instead of `-q`, so that if the check is ever fed from a pipe GNU drains it instead of exiting early, and so that the POSIX `-q` rule cannot turn an unreadable file into status 0 when several files are given.
> > `-e` and `--` protect against a pattern or file name that starts with `-`.
> > Keep the regex within POSIX ERE (no `\d`, no `\b`) because GNU, Apple `REG_ENHANCED`, and musl disagree on backslash escapes outside the standard.
> >
> > Follow-up: When would you replace this with a small program instead of tightening the grep call further?

> [!question] L7+: Why does GNU grep compare the inode of standard output with every input file, and what does it cost the other implementations not to?
>
> > [!success]- Answer
> > If stdout is a regular file that is also an input, matched lines appended to it can be read again and matched again, an unbounded loop that ends only when the file system fills.
> > `main` records `out_stat` when stdout is a regular file, and `grepdesc` refuses an input with the same device and inode unless output is quiet, `-l` or `-L`, or `-m 1`, printing `input file is also the output` and returning 2.
> > BSD and BusyBox have no such check; the lab shows them silently copying a small file into itself with status 0, and with output larger than the stdio buffer they can keep reading their own writes.
> > The check is cheap (one `fstat` per file) and turns a silent disk-filling bug into a diagnosable error.
> >
> > Follow-up: Why does GNU skip the check for `-m 1` but not for `-m 2`, according to the comment in `grepdesc`?

> [!question] L7+: A team wants to standardize on `grep -P` for log parsing across Debian and Alpine; what do you tell them?
>
> > [!success]- Answer
> > `-P` exists only in GNU grep; BusyBox and BSD grep do not accept it, so Alpine images would need the GNU `grep` package and macOS developers would need a second grep.
> > GNU `-P` accepts one pattern, searches line by line, refuses multibyte locales other than UTF-8, and can abort a run with status 2 on PCRE2's backtracking, depth, or heap limits.
> > Its `\w`, `\d`, and `\b` semantics have changed between releases (3.9 to 3.11 `NEWS`), so results can differ with the PCRE2 version linked in.
> > If they need captures or lookaround, a Perl or Python step states the dependency honestly; if they need speed on literals, `-F` is faster and portable.
> >
> > Follow-up: How would you detect at run time whether the grep on a host supports `-P`, without parsing `--version`?

## Related

Prerequisites: [[ReDoS]]

Next: [[ripgrep-Internals]]

See also: [[BRE-ERE-and-PCRE]], [[POSIX-Leftmost-Longest-Matching]], [[Regex-Engines-NFA-DFA-and-Backtracking]], [[Locale-and-LC_ALL]], [[Unicode-and-UTF-8-in-Text-Tools]], [[sed-Pattern-and-Hold-Space]], [[awk-Language]], [[Perl-One-Liners]], [[SIGPIPE-and-EPIPE]], [[pipefail-and-PIPESTATUS]], [[errexit-Semantics-and-Exceptions]], [[Pathname-Expansion]], [[Terminal-Escape-Injection]], [[File-Descriptors-and-Open-File-Descriptions]]

## Further reading

- [POSIX.1-2024 XCU grep](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/grep.html): the required options, the newline-separated pattern list, and the `-q` exit rule.
- [POSIX.1-2024 XBD 9 Regular Expressions](https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap09.html): which backslash escapes are undefined and what a NUL in matched text may mean.
- [POSIX.1-2024 XBD 12.2 Utility Syntax Guidelines](https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap12.html): Guidelines 9 and 10, the basis for `--`.
- [POSIX.1-2024 XSH exit](https://pubs.opengroup.org/onlinepubs/9799919799/functions/exit.html): why a stdio-based grep can still leave a correct file offset for the next reader.
- [GNU grep 3.12 release tarball](https://ftpmirror.gnu.org/grep/grep-3.12.tar.xz): `src/grep.c`, `src/dfasearch.c`, `src/kwset.c`, `src/pcresearch.c`, `lib/dfa.c`, `doc/grep.texi`, and `NEWS` are the primary record for every GNU claim here.
- [Mike Haertel, "why GNU grep is fast" (2010)](https://lists.freebsd.org/pipermail/freebsd-current/2010-August/019310.html): the original author's account of the buffer-first design.
- [FreeBSD grep `file.c`](https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/file.c) and [`util.c`](https://raw.githubusercontent.com/freebsd/freebsd-src/main/usr.bin/grep/util.c): the 32 KiB line reader and the per-line `regexec` loop.
- [Apple text_cmds `grep.c`](https://raw.githubusercontent.com/apple-oss-distributions/text_cmds/main/grep/grep.c): `REG_ENHANCED`, `LINK_SKIP`, and the macOS-only changes.
- [BusyBox `findutils/grep.c`](https://raw.githubusercontent.com/mirror/busybox/master/findutils/grep.c): a complete grep in about 900 lines, and what it leaves out.
- [musl `__stdio_exit.c`](https://git.musl-libc.org/cgit/musl/plain/src/stdio/__stdio_exit.c): the five lines that make BusyBox `-m` leave the right offset.
- [CVE-2012-5667](https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2012-5667) and [CVE-2015-1345](https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2015-1345): the long-line overflow and the `-F` over-read.
