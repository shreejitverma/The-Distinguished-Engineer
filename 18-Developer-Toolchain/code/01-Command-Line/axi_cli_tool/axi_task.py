#!/usr/bin/env python3
"""Reference implementation of a dual-mode CLI following clig.dev and the 10 AXI principles.

Author: Developer Toolchain Curriculum
Target: POSIX systems (Linux, macOS)
Dependencies: Python 3.8+ standard library only
"""

import os
import sys
import json
import signal
from typing import Any, Dict, List, Optional

# Restore default SIGPIPE handler so CLI exits cleanly when downstream pipe closes
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

# Standard sysexits.h exit codes
EX_OK = 0
EX_USAGE = 64        # Command line usage error
EX_DATAERR = 65      # Data format error
EX_NOINPUT = 66      # Cannot open input / resource not found
EX_SOFTWARE = 70     # Internal software error

DEFAULT_TASKS = [
    {
        "id": 1,
        "title": "Audit SSH bastion host configs",
        "status": "open",
        "priority": "high",
        "description": "Perform comprehensive audit of /etc/ssh/sshd_config across fleet.\n"
                       "Enforce HostKeyAlgorithms and KexAlgorithms per security policy.\n"
                       "Disable interactive password authentication and root login.\n"
                       "Ensure AuthorizedKeysCommand verifies cryptographic certificates.\n"
                       "Deploy automated verification probes across all staging zones."
    },
    {
        "id": 2,
        "title": "Migrate pipeline to Nix flakes",
        "status": "open",
        "priority": "medium",
        "description": "Pin compiler toolchains and system dependencies in flake.nix.\n"
                       "Configure binary cache substitution to reduce CI build duration."
    },
    {
        "id": 3,
        "title": "Fix broken pipe in log exporter",
        "status": "closed",
        "priority": "low",
        "description": "Reset SIGPIPE to SIG_DFL in Python exporter daemon.\n"
                       "Prevent BrokenPipeError tracebacks when piped to head."
    }
]


def determine_color_support(stream, explicit_flag: Optional[str] = None) -> bool:
    """Evaluate color support following strict precedence hierarchy."""
    if explicit_flag == "always":
        return True
    if explicit_flag == "never":
        return False

    # 1. NO_COLOR: any non-empty value disables ANSI styling
    no_color = os.environ.get("NO_COLOR")
    if no_color is not None and len(no_color) > 0:
        return False

    # 2. FORCE_COLOR or CLICOLOR_FORCE: non-zero value forces styling
    force_color = os.environ.get("FORCE_COLOR")
    if force_color is not None and force_color != "0":
        return True
    clicolor_force = os.environ.get("CLICOLOR_FORCE")
    if clicolor_force is not None and clicolor_force != "0":
        return True

    # 3. CLICOLOR: if set to 0, disable; otherwise defer to TTY
    clicolor = os.environ.get("CLICOLOR")
    if clicolor == "0":
        return False

    # 4. TERM=dumb disables color
    if os.environ.get("TERM") == "dumb":
        return False

    # 5. Default fallback to isatty
    try:
        return stream.isatty()
    except Exception:
        return False


def load_db(db_path: Optional[str]) -> List[Dict[str, Any]]:
    if not db_path or not os.path.exists(db_path):
        return [dict(t) for t in DEFAULT_TASKS]
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error loading database {db_path}: {e}\n")
        sys.exit(EX_DATAERR)


def save_db(db_path: Optional[str], data: List[Dict[str, Any]]) -> None:
    if not db_path:
        return
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def emit_structured_error(code: str, message: str, exit_code: int) -> None:
    """Principle 6: Structured errors on stderr with distinct exit code."""
    payload = {
        "error": code,
        "message": message,
        "exit_code": exit_code
    }
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.exit(exit_code)


def truncate_text(text: str, max_lines: int = 1, max_chars: int = 60) -> str:
    """Principle 3: Content truncation with explicit size hint."""
    lines = text.strip().split("\n")
    total_lines = len(lines)
    first_line = lines[0]

    if total_lines <= max_lines and len(first_line) <= max_chars:
        return text

    truncated_sample = first_line[:max_chars]
    omitted_lines = total_lines - 1
    hint = f" [truncated {omitted_lines} lines; use --full to view all {total_lines} lines]"
    return truncated_sample + hint


_TOON_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_TOON_RESERVED = {"true", "false", "null"}


def toon_scalar(value: Any, delimiter: str = ",") -> str:
    """Encode a string for TOON (spec v4.4): quote when the bare form would be ambiguous, escape inside quotes."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)  # a real number stays bare; only numeric-looking strings are quoted
    text = str(value)
    needs_quote = (
        text == ""
        or text != text.strip()
        or text in _TOON_RESERVED
        or text.startswith("-")
        or any(c in text for c in (delimiter, ":", '"', "\\", "[", "]", "{", "}"))
        or any(ord(c) < 0x20 for c in text)
    )
    if not needs_quote:
        try:
            float(text)
            needs_quote = True  # would decode as a number
        except ValueError:
            pass
    if not needs_quote:
        return text
    return '"' + "".join(_TOON_ESCAPES.get(c, c) for c in text) + '"'


def render_toon_list(tasks: List[Dict[str, Any]], total_all: int, open_count: int, closed_count: int,
                     filter_expr: Optional[str] = None) -> str:
    """Principle 1 & 4 & 5: TOON format with pre-computed aggregates and definitive empty states."""
    lines = []
    # Pre-computed aggregates in header
    lines.append(f"summary(total:{total_all},open:{open_count},closed:{closed_count})")

    count = len(tasks)
    schema = "{id,title,status,priority}"
    if count == 0:
        # Principle 5: Definitive empty state. TOON v4.4 encodes an empty array as `key: []`
        # (the legacy `key[0]:` form must not be emitted), so the confirmation is a separate key.
        suffix = f" matching filter {filter_expr}" if filter_expr else ""
        lines.append("tasks: []")
        lines.append(f"result: 0 tasks{suffix}")
        return "\n".join(lines)

    lines.append(f"tasks[{count}]{schema}:")
    for t in tasks:
        # Principle 2: Minimal default schema (4 fields)
        fields = [t["id"], t["title"], t["status"], t["priority"]]
        lines.append("  " + ",".join(toon_scalar(f) for f in fields))
    return "\n".join(lines)


def render_toon_item(t: Dict[str, Any], full: bool = False) -> str:
    """Render single item in TOON; multi-line text is a quoted string with \\n escapes (TOON has no block scalar)."""
    desc = t.get("description", "")
    if not full:
        desc = truncate_text(desc, max_lines=1, max_chars=60)
    lines = [
        "task:",
        f"  id: {t['id']}",
        f"  title: {toon_scalar(t['title'])}",
        f"  status: {toon_scalar(t['status'])}",
        f"  priority: {toon_scalar(t['priority'])}",
        f"  description: {toon_scalar(desc)}",
    ]
    return "\n".join(lines)


def render_human_list(tasks: List[Dict[str, Any]], color: bool = False) -> str:
    if not tasks:
        return "No tasks found."
    lines = []
    header = f"{'ID':<4} {'STATUS':<8} {'PRIORITY':<8} {'TITLE'}"
    if color:
        header = f"\033[1m{header}\033[0m"
    lines.append(header)
    for t in tasks:
        status_str = t['status']
        if color:
            if status_str == "open":
                status_str = f"\033[32m{status_str}\033[0m"
            else:
                status_str = f"\033[90m{status_str}\033[0m"
        pad = 17 if color else 8
        lines.append(f"{t['id']:<4} {status_str:<{pad}} {t['priority']:<8} {t['title']}")
    return "\n".join(lines)


def print_help() -> None:
    """Principle 10: concise reference with defaults and examples (AXI sets no line budget)."""
    help_text = """axi-task - agent-friendly task and workflow manager

Usage:
  axi-task [command] [options]

Commands:
  (none), list    List tasks with aggregates (content-first default)
  show <id>       Display detailed information for a task
  create          Create a new task (--title required)
  close <id>      Mark a task as closed (idempotent)
  delete <id>     Remove a task (--ignore-missing makes a missing id a no-op)

Options:
  --db=PATH       Path to database JSON file (default: built-in sample tasks)
  --format=FMT    toon | json | human (default: human on a TTY, toon otherwise)
  --status=STATUS Filter tasks: all (default), open, closed
  --full          Display untruncated content bodies
  --color=MODE    auto (default), always, never
  --help          Show this reference

Exit status: 0 success, 64 usage error, 65 bad data, 66 not found.
Errors are one JSON line on stderr.

Examples:
  axi-task                          # live task list, no help text
  axi-task --status=closed          # filtered list; an empty result says so
  axi-task show 1 --full            # complete description of task 1
"""
    sys.stdout.write(help_text)


def main() -> None:
    raw_args = sys.argv[1:]

    if "--help" in raw_args or "-h" in raw_args:
        print_help()
        sys.exit(EX_OK)

    # Global options
    db_path = None
    output_format = None
    color_flag = None
    full_output = False
    status_filter = "all"
    cmd = None
    cmd_args = []

    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
        elif arg.startswith("--format="):
            output_format = arg.split("=", 1)[1]
        elif arg.startswith("--color="):
            color_flag = arg.split("=", 1)[1]
        elif arg.startswith("--status="):
            status_filter = arg.split("=", 1)[1]
        elif arg == "--full":
            full_output = True
        elif not arg.startswith("-") and cmd is None:
            cmd = arg
            cmd_args = raw_args[i + 1:]
            break
        elif arg.startswith("-"):
            emit_structured_error("ERR_UNKNOWN_FLAG", f"Unrecognized option: {arg}", EX_USAGE)
        else:
            emit_structured_error("ERR_UNEXPECTED_ARG", f"Unexpected argument: {arg}", EX_USAGE)
        i += 1

    # Principle 8: Content first; default to 'list' if no command given
    if cmd is None:
        cmd = "list"

    # Separate options from positional arguments inside cmd_args
    sub_positional = []
    ignore_missing = False
    title = None
    priority = "medium"
    description = ""

    for ca in cmd_args:
        if ca.startswith("--db="):
            db_path = ca.split("=", 1)[1]
        elif ca.startswith("--format="):
            output_format = ca.split("=", 1)[1]
        elif ca.startswith("--color="):
            color_flag = ca.split("=", 1)[1]
        elif ca.startswith("--status="):
            status_filter = ca.split("=", 1)[1]
        elif ca == "--full":
            full_output = True
        elif ca == "--ignore-missing":
            ignore_missing = True
        elif ca.startswith("--title="):
            title = ca.split("=", 1)[1]
        elif ca.startswith("--priority="):
            priority = ca.split("=", 1)[1]
        elif ca.startswith("--description="):
            description = ca.split("=", 1)[1]
        elif ca.startswith("-"):
            emit_structured_error("ERR_UNKNOWN_FLAG", f"Unrecognized option: {ca}", EX_USAGE)
        else:
            sub_positional.append(ca)

    # Determine output format and coloring
    use_color = determine_color_support(sys.stdout, color_flag)
    if output_format is None:
        if sys.stdout.isatty():
            output_format = "human"
        else:
            output_format = "toon"

    tasks = load_db(db_path)

    if cmd == "list":
        # Pre-computed aggregates (Principle 4)
        total_all = len(tasks)
        open_count = sum(1 for t in tasks if t.get("status") == "open")
        closed_count = sum(1 for t in tasks if t.get("status") == "closed")

        filtered = tasks
        if status_filter != "all":
            filtered = [t for t in tasks if t.get("status") == status_filter]

        if output_format == "json":
            out = {
                "summary": {"total": total_all, "open": open_count, "closed": closed_count},
                "tasks": filtered
            }
            sys.stdout.write(json.dumps(out, indent=2) + "\n")
        elif output_format == "human":
            sys.stdout.write(render_human_list(filtered, color=use_color) + "\n")
        else:  # toon
            filter_expr = f"status={status_filter}" if status_filter != "all" else None
            sys.stdout.write(render_toon_list(filtered, total_all, open_count, closed_count, filter_expr) + "\n")

    elif cmd == "show":
        if not sub_positional:
            emit_structured_error("ERR_MISSING_ARG", "Task ID required for show", EX_USAGE)
        try:
            task_id = int(sub_positional[0])
        except ValueError:
            emit_structured_error("ERR_INVALID_ID", f"Task ID must be an integer: {sub_positional[0]}", EX_DATAERR)

        matched = next((t for t in tasks if t["id"] == task_id), None)
        if not matched:
            emit_structured_error("ERR_NOT_FOUND", f"Task {task_id} not found", EX_NOINPUT)

        if output_format == "json":
            sys.stdout.write(json.dumps(matched, indent=2) + "\n")
        else:
            sys.stdout.write(render_toon_item(matched, full=full_output) + "\n")

    elif cmd == "create":
        if not title:
            # Principle 6: Fail fast without interactive prompting
            emit_structured_error("ERR_MISSING_REQUIRED_FIELD", "Missing required flag: --title", EX_USAGE)

        next_id = max([t["id"] for t in tasks], default=0) + 1
        new_task = {
            "id": next_id,
            "title": title,
            "status": "open",
            "priority": priority,
            "description": description
        }
        tasks.append(new_task)
        save_db(db_path, tasks)

        # Output created task
        if output_format == "json":
            sys.stdout.write(json.dumps(new_task, indent=2) + "\n")
        else:
            sys.stdout.write(f"created: task:{next_id}\n")

        # Principle 9: Contextual disclosure (Next-step suggestions)
        sys.stderr.write(f"Next: run 'axi-task show {next_id}' to inspect or 'axi-task close {next_id}' to complete\n")

    elif cmd == "close":
        if not sub_positional:
            emit_structured_error("ERR_MISSING_ARG", "Task ID required for close", EX_USAGE)
        try:
            task_id = int(sub_positional[0])
        except ValueError:
            emit_structured_error("ERR_INVALID_ID", f"Task ID must be an integer: {sub_positional[0]}", EX_DATAERR)

        matched = next((t for t in tasks if t["id"] == task_id), None)
        if not matched:
            emit_structured_error("ERR_NOT_FOUND", f"Task {task_id} not found", EX_NOINPUT)

        matched["status"] = "closed"
        save_db(db_path, tasks)
        sys.stdout.write(f"closed: task:{task_id}\n")

    elif cmd == "delete":
        if not sub_positional:
            emit_structured_error("ERR_MISSING_ARG", "Task ID required for delete", EX_USAGE)
        try:
            task_id = int(sub_positional[0])
        except ValueError:
            emit_structured_error("ERR_INVALID_ID", f"Task ID must be an integer: {sub_positional[0]}", EX_DATAERR)

        before_len = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) == before_len and not ignore_missing:
            emit_structured_error("ERR_NOT_FOUND", f"Task {task_id} not found", EX_NOINPUT)

        save_db(db_path, tasks)
        sys.stdout.write(f"deleted: task:{task_id}\n")

    else:
        emit_structured_error("ERR_UNKNOWN_COMMAND", f"Unknown command: {cmd}", EX_USAGE)


if __name__ == "__main__":
    main()
