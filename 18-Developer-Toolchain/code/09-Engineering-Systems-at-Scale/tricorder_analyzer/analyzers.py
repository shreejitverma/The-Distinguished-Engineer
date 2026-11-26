"""Concrete analyzers for the toy Tricorder. Each one is small on purpose: the paper
stresses that the checks that pay off are simple, and that the platform is the product."""
from __future__ import annotations

import ast
import builtins
import re
import symtable

from tricorder import (COMPILATION, FILES, MACHINE, MAYBE, Analyzer, Edit, Fix, Note, Source)

URL = "https://example.invalid/checks/"
LINE_LIMIT = 100


def _exception_names():
    return {n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException)}


EXC_NAMES = _exception_names()


def _local_bindings(table):
    names = {symbol.get_name() for symbol in table.get_symbols()
             if symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()}
    for child in table.get_children():
        names.update(_local_bindings(child))
    return names


def _exception_definitions(path, text, tree):
    """Recognize bare builtins and direct module-local subclasses in a small static subset."""
    nodes = list(ast.walk(tree))
    if any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names) for node in nodes):
        return {}
    try:
        module = symtable.symtable(text, path, "exec")
    except SyntaxError:
        return {}
    bound = _local_bindings(module)
    definitions = dict.fromkeys(EXC_NAMES - bound, 0)
    # A custom class has one expected binding: its own declaration.
    other_writes = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
    other_writes.update(node.name for node in nodes if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)))
    other_writes.update(node.rest for node in nodes if isinstance(node, ast.MatchMapping))
    for child in module.get_children():
        other_writes.update(_local_bindings(child))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.decorator_list or node.keywords or len(node.bases) != 1:
            continue
        symbol = module.lookup(node.name)
        base = node.bases[0]
        if (not symbol.is_imported() and len(symbol.get_namespaces()) == 1 and node.name not in other_writes
                and isinstance(base, ast.Name) and base.id in definitions):
            definitions[node.name] = node.end_lineno
    return definitions


class LineTooLong(Analyzer):
    category = "LineTooLong"
    stage = FILES

    def analyze(self, path, text, tree):
        src = Source(text)
        return [Note(self.category, path, i, LINE_LIMIT, "line is %d characters, limit %d" % (len(l), LINE_LIMIT),
                     URL + self.category, l) for i, l in enumerate(src.lines, 1) if len(l) > LINE_LIMIT]


class TrailingWhitespace(Analyzer):
    category = "TrailingWhitespace"
    stage = FILES

    def analyze(self, path, text, tree):
        src, notes = Source(text), []
        for i, line in enumerate(src.lines, 1):
            stripped = line.rstrip(" \t")
            if stripped != line:
                start = src.offset(i, len(stripped))
                fix = Fix("Delete trailing whitespace", (Edit(start, start + len(line) - len(stripped), ""),),
                          MACHINE, ast_preserving=True)
                notes.append(Note(self.category, path, i, len(stripped), "trailing whitespace",
                                  URL + self.category, line, (fix,)))
        return notes


class DeadException(Analyzer):
    """Python analogue of Error Prone DeadException: an exception built and dropped."""
    category = "DeadException"
    stage = COMPILATION
    correctness_only = True

    def analyze(self, path, text, tree):
        src, notes = Source(text), []
        definitions = _exception_definitions(path, text, tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id in definitions
                    and node.lineno > definitions[node.value.func.id]):
                start = src.offset(node.value.lineno, node.value.col_offset)
                fix = Fix("Did you mean 'raise %s(...)'?" % node.value.func.id, (Edit(start, start, "raise "),), MACHINE)
                notes.append(Note(self.category, path, node.lineno, node.col_offset,
                                  "exception created but not raised", URL + self.category,
                                  src.line_text(node.lineno), (fix,)))
        return notes


class NoneComparison(Analyzer):
    """== None to is None, as a minimal edit on the operator token only.
    Marked maybe-incorrect: an overloaded __eq__ can make the two differ."""
    category = "NoneComparison"
    stage = COMPILATION

    def analyze(self, path, text, tree):
        src, notes = Source(text), []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            prev = node.left
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
                    gap_start = src.offset(prev.end_lineno, prev.end_col_offset)
                    gap_end = src.offset(right.lineno, right.col_offset)
                    m = re.search(r"==|!=", text[gap_start:gap_end])
                    if m:
                        s = gap_start + m.start()
                        new = "is" if isinstance(op, ast.Eq) else "is not"
                        fix = Fix("Use '%s' for the None comparison" % new, (Edit(s, s + 2, new),), MAYBE)
                        notes.append(Note(self.category, path, right.lineno, right.col_offset,
                                          "comparison to None should use identity", URL + self.category,
                                          src.line_text(right.lineno), (fix,)))
                prev = right
        return notes


class MutableDefaultArgument(Analyzer):
    """No fix is offered: the safe rewrite needs a design decision, so the message says how."""
    category = "MutableDefaultArgument"
    stage = COMPILATION
    correctness_only = True

    def analyze(self, path, text, tree):
        src, notes = Source(text), []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in node.args.defaults + [k for k in node.args.kw_defaults if k is not None]:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                        notes.append(Note(self.category, path, d.lineno, d.col_offset,
                                          "mutable default is shared by every call; default to None and build it inside",
                                          URL + self.category, src.line_text(d.lineno)))
        return notes


class UnusedImport(Analyzer):
    category = "UnusedImport"
    stage = COMPILATION

    def __init__(self, guard_sole_statement=True):
        self.guard = guard_sole_statement

    def analyze(self, path, text, tree):
        src, notes = Source(text), []
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                used |= {e.value for e in ast.walk(node.value) if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        parents = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                parents[c] = p
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in used or alias.name == "*":
                    continue
                fixes = ()
                line = src.line_text(node.lineno)
                whole_line = node.lineno == node.end_lineno and line.strip() == ast.get_source_segment(text, node)
                if len(node.names) == 1 and whole_line:
                    start = src.offset(node.lineno, 0)
                    end = start + len(line) + 1
                    parent = parents.get(node)
                    body = getattr(parent, "body", [])
                    only = isinstance(body, list) and len(body) == 1 and body[0] is node
                    if only and self.guard:
                        indent = line[: len(line) - len(line.lstrip())]
                        fixes = (Fix("Replace the only statement with pass", (Edit(start, end, indent + "pass\n"),), MACHINE),)
                    else:
                        fixes = (Fix("Delete the unused import", (Edit(start, end, ""),), MACHINE),)
                notes.append(Note(self.category, path, node.lineno, node.col_offset,
                                  "'%s' is imported but unused" % bound, URL + self.category, line, fixes))
        return notes


REGISTRY = {
    "LineTooLong": LineTooLong,
    "TrailingWhitespace": TrailingWhitespace,
    "DeadException": DeadException,
    "NoneComparison": NoneComparison,
    "MutableDefaultArgument": MutableDefaultArgument,
    "UnusedImport": UnusedImport,
}


def build(names=None):
    return [REGISTRY[n]() for n in (names or REGISTRY)]


def affected_modules(changed, corpus):
    """DEPS stage: which files import a changed module. Informational, so no fix and no Please-fix."""
    out = []
    for path, text in sorted(corpus.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m.split(".")[0] + ".py" in changed for m in mods):
                out.append(path)
                break
    return out
