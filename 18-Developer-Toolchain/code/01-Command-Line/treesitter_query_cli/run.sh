#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0

python3 "${SCRIPT_DIR}/treesitter_query_cli.py" dump-tree "${SCRIPT_DIR}/example.c"
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '(function_definition name: (identifier) @fn.name)'
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '(parameter_declaration type: (primitive_type) @param.type name: (identifier) @param.name)'
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '(function_definition name: (identifier) @fn.name (#eq? @fn.name "process_transaction"))'
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '((identifier) @tx_var (#match? @tx_var "^tx_"))'
echo
# The pre-fix form puts the capture inside the parentheses; real Tree-sitter rejects it, so must the lab.
if python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '(identifier @tx_var (#match? @tx_var "^tx_"))' 2>&1; then
    echo "UNEXPECTED: invalid query accepted"
else
    echo "exit status: $?"
fi
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" query "${SCRIPT_DIR}/example.c" \
    -q '(function_definition body: (compound_statement) @function.inner) @function.outer'
echo
python3 "${SCRIPT_DIR}/treesitter_query_cli.py" demo-incremental "${SCRIPT_DIR}/example.c"
