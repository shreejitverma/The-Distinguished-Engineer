#!/usr/bin/env bash
#
# Shell Startup Matrix and Comparative Linguistic Verification.
# Evaluates POSIX sh (dash), Bash, Zsh, and Fish execution cascades and syntax semantics.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "${SANDBOX}"' EXIT

export LC_ALL=C
export TERM_PROGRAM=""
export SHELL_SESSION_HISTORY=0
export NOSYSBASHRC=1

# The harness has no "requires: zsh" tag, so a missing tool is a loud failure here, never a silent skip.
for tool in bash zsh dash fish python3; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "shell_startup_matrix: required tool not on PATH: ${tool}" >&2
        exit 2
    fi
done

# Resolve binary paths to ensure reproducible invocation
BASH_BIN="$(command -v bash)"
ZSH_BIN="$(command -v zsh)"
DASH_BIN="$(command -v dash)"
FISH_BIN="$(command -v fish)"

echo "=== SHELL PLATFORM IDENTIFICATION ==="
echo "Host OS: $(uname -s)"
echo "Host Arch: $(uname -m)"
# Exact versions go to stderr (not compared); stdout pins only the floors this lab needs,
# so a Homebrew or Nix upgrade within the same major version does not break the golden file.
BASH_VERSION_FOUND="$("${BASH_BIN}" -c 'echo "${BASH_VERSION}"')"
ZSH_VERSION_FOUND="$("${ZSH_BIN}" -c 'echo "${ZSH_VERSION}"')"
FISH_VERSION_FOUND="$("${FISH_BIN}" -c 'echo "$version"')"
echo "versions: bash ${BASH_VERSION_FOUND} zsh ${ZSH_VERSION_FOUND} fish ${FISH_VERSION_FOUND}" >&2

require_major() {
    # require_major <name> <found version> <minimum major>
    local major="${2%%.*}"
    if [ "${major}" -lt "$3" ] 2>/dev/null; then
        echo "shell_startup_matrix: $1 ${2} is older than the required major version $3" >&2
        exit 2
    fi
    echo "$1 major version >= $3: yes"
}
require_major bash "${BASH_VERSION_FOUND}" 5
require_major zsh "${ZSH_VERSION_FOUND}" 5
require_major fish "${FISH_VERSION_FOUND}" 4

# ==============================================================================
# PART 1: STARTUP FILE EXECUTION CASCADES
# ==============================================================================
echo ""
echo "=== PART 1: STARTUP FILE EXECUTION CASCADES ==="

# 1.1 Bash Cascade Setup
BASH_SANDBOX="${SANDBOX}/bash_env"
mkdir -p "${BASH_SANDBOX}"
cat << 'EOF' > "${BASH_SANDBOX}/.bash_profile"
echo "  [bash] sourced ~/.bash_profile (login)"
EOF
cat << 'EOF' > "${BASH_SANDBOX}/.bashrc"
echo "  [bash] sourced ~/.bashrc (interactive)"
EOF
cat << 'EOF' > "${BASH_SANDBOX}/custom_rcfile"
echo "  [bash] sourced the --rcfile file (not ~/.bashrc)"
EOF
cat << 'EOF' > "${BASH_SANDBOX}/.bashenv"
echo "  [bash] sourced BASH_ENV file (non-interactive)"
EOF

echo "--- 1.1 Bash Execution Modes ---"
echo "Mode A: Interactive with explicit --rcfile (--noprofile)"
HOME="${BASH_SANDBOX}" "${BASH_BIN}" --noprofile --rcfile "${BASH_SANDBOX}/custom_rcfile" -i -c "exit" 2>/dev/null || true
echo "Mode B: Login (via HOME .bash_profile)"
HOME="${BASH_SANDBOX}" "${BASH_BIN}" --login -c "exit" 2>/dev/null || true
echo "Mode C: Non-login Interactive"
HOME="${BASH_SANDBOX}" "${BASH_BIN}" -i -c "exit" 2>/dev/null || true
echo "Mode D: Non-login Non-interactive (with BASH_ENV)"
HOME="${BASH_SANDBOX}" BASH_ENV="${BASH_SANDBOX}/.bashenv" "${BASH_BIN}" -c "true" 2>/dev/null || true
echo "Mode E: Non-login Non-interactive (default: no startup files)"
HOME="${BASH_SANDBOX}" "${BASH_BIN}" -c "true" 2>/dev/null || true

# 1.1b The rshd/sshd rule: a non-interactive `bash -c` whose stdin is a socket sources ~/.bashrc.
# bash decides with getpeername(2) on stdin (isnetconn), so a connected socketpair reproduces it;
# SHLVL=0 mimics sshd's child, which starts with no SHLVL (bash only applies the rule at shell level < 2).
BASH_REMOTE="${SANDBOX}/bash_remote"
mkdir -p "${BASH_REMOTE}"
cat << 'EOF' > "${BASH_REMOTE}/.bashrc"
echo "  [bash] sourced ~/.bashrc (remote non-interactive rule)"
EOF

# run_with_stdin <socket|pipe> <command...>: run the command with stdin of that kind.
run_with_stdin() {
    python3 -I -c '
import socket, subprocess, sys
kind, cmd = sys.argv[1], sys.argv[2:]
if kind == "socket":
    a, b = socket.socketpair()
    stdin = b
else:
    stdin = subprocess.PIPE
subprocess.run(cmd, stdin=stdin, check=False)
' "$@"
}

echo "--- 1.1b Bash Remote Shell Rule (stdin kind) ---"
echo "Mode F: stdin is a socket (like sshd)"
HOME="${BASH_REMOTE}" SHLVL=0 run_with_stdin socket "${BASH_BIN}" -c "true" 2>/dev/null || true
echo "Mode G: stdin is a socket, with --norc"
HOME="${BASH_REMOTE}" SHLVL=0 run_with_stdin socket "${BASH_BIN}" --norc -c "true" 2>/dev/null || true
echo "Mode H: stdin is a pipe (no network connection)"
HOME="${BASH_REMOTE}" SHLVL=0 run_with_stdin pipe "${BASH_BIN}" -c "true" 2>/dev/null || true
echo "Mode I: stdin is a socket, invoked as sh"
ln -s "${BASH_BIN}" "${BASH_REMOTE}/sh"
HOME="${BASH_REMOTE}" SHLVL=0 run_with_stdin socket "${BASH_REMOTE}/sh" -c "true" 2>/dev/null || true

# 1.2 Zsh Cascade Setup
ZSH_SANDBOX="${SANDBOX}/zsh_env"
mkdir -p "${ZSH_SANDBOX}"
cat << 'EOF' > "${ZSH_SANDBOX}/.zshenv"
echo "  [zsh] sourced ~/.zshenv (all invocations)"
EOF
cat << 'EOF' > "${ZSH_SANDBOX}/.zprofile"
echo "  [zsh] sourced ~/.zprofile (login before zshrc)"
EOF
cat << 'EOF' > "${ZSH_SANDBOX}/.zshrc"
echo "  [zsh] sourced ~/.zshrc (interactive)"
EOF
cat << 'EOF' > "${ZSH_SANDBOX}/.zlogin"
echo "  [zsh] sourced ~/.zlogin (login after zshrc)"
EOF

echo ""
echo "--- 1.2 Zsh Execution Modes ---"
echo "Mode A: Login + Interactive"
ZDOTDIR="${ZSH_SANDBOX}" "${ZSH_BIN}" --login -i -c "exit" 2>/dev/null || true
echo "Mode B: Login + Non-interactive"
ZDOTDIR="${ZSH_SANDBOX}" "${ZSH_BIN}" --login -c "exit" 2>/dev/null || true
echo "Mode C: Non-login Interactive"
ZDOTDIR="${ZSH_SANDBOX}" "${ZSH_BIN}" -i -c "exit" 2>/dev/null || true
echo "Mode D: Non-login Non-interactive (script execution)"
ZDOTDIR="${ZSH_SANDBOX}" "${ZSH_BIN}" -c "true" 2>/dev/null || true
echo "Mode E: Explicit Bypass (-f / NO_RCS)"
ZDOTDIR="${ZSH_SANDBOX}" "${ZSH_BIN}" -f -c "true" 2>/dev/null || true

# 1.3 Fish Cascade Setup
FISH_SANDBOX="${SANDBOX}/fish_env"
mkdir -p "${FISH_SANDBOX}/fish/conf.d"
cat << 'EOF' > "${FISH_SANDBOX}/fish/conf.d/01_snippet.fish"
echo "  [fish] sourced conf.d/01_snippet.fish"
EOF
cat << 'EOF' > "${FISH_SANDBOX}/fish/config.fish"
echo "  [fish] sourced config.fish"
if status is-login
    echo "  [fish] branch evaluated: status is-login"
end
if status is-interactive
    echo "  [fish] branch evaluated: status is-interactive"
end
EOF

echo ""
echo "--- 1.3 Fish Execution Modes ---"
echo "Mode A: Interactive Non-login"
XDG_CONFIG_HOME="${FISH_SANDBOX}" "${FISH_BIN}" -i -c "exit" 2>/dev/null || true
echo "Mode B: Non-interactive Non-login (script)"
XDG_CONFIG_HOME="${FISH_SANDBOX}" "${FISH_BIN}" -c "true" 2>/dev/null || true
echo "Mode C: Login Non-interactive"
XDG_CONFIG_HOME="${FISH_SANDBOX}" "${FISH_BIN}" --login -c "true" 2>/dev/null || true

# 1.4 Dash / POSIX sh Cascade Setup
DASH_SANDBOX="${SANDBOX}/dash_env"
mkdir -p "${DASH_SANDBOX}"
cat << 'EOF' > "${DASH_SANDBOX}/.profile"
echo "  [dash/posix] sourced ~/.profile (login)"
EOF
cat << 'EOF' > "${DASH_SANDBOX}/.posix_env"
echo "  [dash/posix] sourced \$ENV (interactive)"
EOF

echo ""
echo "--- 1.4 POSIX sh / Dash Execution Modes ---"
echo "Mode A: Login Shell"
HOME="${DASH_SANDBOX}" "${DASH_BIN}" -l -c "exit" 2>/dev/null || true
echo "Mode B: Interactive Non-login with ENV"
ENV="${DASH_SANDBOX}/.posix_env" "${DASH_BIN}" -i -c "exit" 2>/dev/null || true
echo "Mode C: Non-interactive Script (ignores ENV and .profile)"
HOME="${DASH_SANDBOX}" ENV="${DASH_SANDBOX}/.posix_env" "${DASH_BIN}" -c "true" 2>/dev/null || true

# ==============================================================================
# PART 2: ARRAY INDEXING AND SLICING DIVERGENCE
# ==============================================================================
echo ""
echo "=== PART 2: ARRAY INDEXING AND SLICING DIVERGENCE ==="

"${BASH_BIN}" -c '
arr=(elem0 elem1 elem2 elem3)
echo "Bash base index 0       : ${arr[0]}"
echo "Bash index 1            : ${arr[1]}"
echo "Bash array length       : ${#arr[@]}"
echo "Bash slice offset:len   : ${arr[@]:1:2}"
'

"${ZSH_BIN}" -c '
arr=(elem1 elem2 elem3 elem4)
echo "Zsh default base index 1: $arr[1]"
echo "Zsh index 0 (empty)     : $arr[0]"
echo "Zsh array length        : $#arr"
echo "Zsh slice [start,end]   : $arr[1,2]"
setopt KSH_ARRAYS
echo "Zsh with KSH_ARRAYS [0] : ${arr[0]}"
echo "Zsh with KSH_ARRAYS bare: $arr"
'

"${FISH_BIN}" -c '
set arr elem1 elem2 elem3 elem4
echo "Fish base index 1       : "$arr[1]
echo "Fish index 2            : "$arr[2]
echo "Fish array count        : "(count $arr)
echo "Fish slice [start..end] : "(string join " " $arr[2..3])
'

"${DASH_BIN}" -c '
set -- elem1 elem2 elem3 elem4
echo "Dash positional param \$1: $1"
echo "Dash positional param \$2: $2"
echo "Dash positional count \$#: $#"
'

# ==============================================================================
# PART 3: WORD SPLITTING AND PATHNAME EXPANSION (GLOBBING)
# ==============================================================================
echo ""
echo "=== PART 3: WORD SPLITTING AND GLOB EXPANSION ==="

GLOB_SANDBOX="${SANDBOX}/glob_env"
mkdir -p "${GLOB_SANDBOX}"
cd "${GLOB_SANDBOX}"
touch alpha.txt beta.txt gamma.log

"${BASH_BIN}" -c '
inspect() { echo "  bash arguments received: $#"; }
str="first second third"
inspect $str
pat="*.txt"
inspect $pat
'

"${ZSH_BIN}" -c '
inspect() { echo "  zsh $1 arguments received: $(($# - 1))"; }
str="first second third"
inspect default $str
pat="*.txt"
inspect default $pat
setopt SH_WORD_SPLIT
inspect SH_WORD_SPLIT $str
'

"${FISH_BIN}" -c '
function inspect; echo "  fish arguments received: "(count $argv); end
set str "first second third"
inspect $str
set pat "*.txt"
inspect $pat
'

"${DASH_BIN}" -c '
inspect() { echo "  dash arguments received: $#"; }
str="first second third"
inspect $str
pat="*.txt"
inspect $pat
'

# ==============================================================================
# PART 4: ZSH GLOB QUALIFIERS VS POSIX / BASH
# ==============================================================================
echo ""
echo "=== PART 4: ZSH GLOB QUALIFIERS ==="

QUAL_SANDBOX="${SANDBOX}/qual_env"
mkdir -p "${QUAL_SANDBOX}/sub_dir"
cd "${QUAL_SANDBOX}"
echo "payload" > regular_file.txt
touch empty_file.txt
ln -s regular_file.txt symlink.txt

"${ZSH_BIN}" -c '
echo "Zsh plain files *(.)    : " *(.)
echo "Zsh directories *(/)    : " *(/)
echo "Zsh symlinks *(@)       : " *(@)
echo "Zsh zero-length *(L0)   : " *(L0)
'

# ==============================================================================
# PART 5: PROGRAMMABLE COMPLETION PARADIGMS
# ==============================================================================
echo ""
echo "=== PART 5: PROGRAMMABLE COMPLETION PARADIGMS ==="

echo "Bash compgen matching:"
"${BASH_BIN}" -c '
compgen -W "build bench bundle test" -- "b"
'

echo "Fish complete query matching:"
"${FISH_BIN}" -c '
complete -c tool -f -a "build" -d "Compile binary target"
complete -c tool -f -a "bench" -d "Execute benchmarks"
complete -c tool -f -a "test"  -d "Run test suite"
complete -C"tool b"
'

# ==============================================================================
# PART 6: STRICT POSIX ENFORCEMENT AND BASHISM FAILURES IN DASH
# ==============================================================================
echo ""
echo "=== PART 6: BASHISM REJECTION IN DASH ==="

echo -n "Test 1: Array syntax in Dash -> "
"${DASH_BIN}" -c 'arr=(1 2 3)' 2>&1 | sed 's|^.*/dash|dash|' | tr '\n' ' ' || true
echo ""

echo -n "Test 2: Double brackets [[ in Dash -> "
"${DASH_BIN}" -c '[[ 1 -eq 1 ]]' 2>&1 | sed 's|^.*/dash|dash|' | tr '\n' ' ' || true
echo ""

echo -n "Test 3: Process substitution <() in Dash -> "
"${DASH_BIN}" -c 'cat <(echo 1)' 2>&1 | sed 's|^.*/dash|dash|' | tr '\n' ' ' || true
echo ""
