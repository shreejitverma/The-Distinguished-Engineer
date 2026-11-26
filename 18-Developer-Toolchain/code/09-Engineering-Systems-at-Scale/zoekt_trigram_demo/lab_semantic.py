"""Lab part 2: why a text index is not a symbol index.

A toy semantic indexer for the Python fixture: it resolves names with the ast module and
emits occurrences keyed by SCIP-shaped symbol strings (grammar in scip.proto, message Symbol).
It then compares "find every parse" through the trigram index with "find references of
Config.parse" through the symbol table.
"""

import ast
import io
import os
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trigram_index import PositionalIndex, substring_search  # noqa: E402

SCHEME, MANAGER, PKG, VERSION = "lab", "python", "demo", "0.1"


def sym(module, *descs):
    return f"{SCHEME} {MANAGER} {PKG} {VERSION} {module}/" + "".join(descs)


def name_col(line_text, start_col, name):
    return line_text.index(name, start_col)


def analyze(sources):
    """sources: {filename: text}. Returns occurrences [(file, line, col, role, symbol)]."""
    modules = {f[:-3]: f for f in sources}
    classes = {}  # (module, Class) -> {method: symbol}
    functions = {}  # (module, name) -> symbol
    trees = {f: ast.parse(t) for f, t in sources.items()}
    occ = []

    for f, tree in trees.items():
        mod = f[:-3]
        lines = sources[f].splitlines()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = {}
                occ.append((f, node.lineno, name_col(lines[node.lineno - 1], node.col_offset, node.name), "definition", sym(mod, node.name + "#")))
                for m in node.body:
                    if isinstance(m, ast.FunctionDef):
                        s = sym(mod, node.name + "#", m.name + "().")
                        methods[m.name] = s
                        occ.append((f, m.lineno, name_col(lines[m.lineno - 1], m.col_offset, m.name), "definition", s))
                classes[(mod, node.name)] = methods
            elif isinstance(node, ast.FunctionDef):
                s = sym(mod, node.name + "().")
                functions[(mod, node.name)] = s
                occ.append((f, node.lineno, name_col(lines[node.lineno - 1], node.col_offset, node.name), "definition", s))

    for f, tree in trees.items():
        mod = f[:-3]
        lines = sources[f].splitlines()
        imports = {}  # local name -> (module, name)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in modules:
                for a in node.names:
                    imports[a.asname or a.name] = (node.module, a.name)

        def resolve_class(name):
            if (mod, name) in classes:
                return (mod, name)
            tgt = imports.get(name)
            return tgt if tgt in classes else None

        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            env = {}
            for a in fn.args.args:
                if isinstance(a.annotation, ast.Name):
                    cls = resolve_class(a.annotation.id)
                    if cls:
                        env[a.arg] = cls
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                    cls = resolve_class(node.value.func.id)
                    if cls and isinstance(node.targets[0], ast.Name):
                        env[node.targets[0].id] = cls
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                fnode = node.func
                if isinstance(fnode, ast.Attribute) and isinstance(fnode.value, ast.Name):
                    cls = env.get(fnode.value.id)
                    if cls and fnode.attr in classes[cls]:
                        col = fnode.end_col_offset - len(fnode.attr)
                        occ.append((f, fnode.lineno, col, "reference", classes[cls][fnode.attr]))
                elif isinstance(fnode, ast.Name):
                    target = (mod, fnode.id) if (mod, fnode.id) in functions else imports.get(fnode.id)
                    if target in functions:
                        occ.append((f, fnode.lineno, fnode.col_offset, "reference", functions[target]))
    return occ


def token_kind(source, line, col):
    """Classify a text hit as comment, string, or code using the tokenizer."""
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING) and tok.start <= (line, col) < tok.end:
            return "comment" if tok.type == tokenize.COMMENT else "string"
    return "code"


def main(fixture_dir):
    names = sorted(n for n in os.listdir(fixture_dir) if n.endswith(".py"))
    sources = {n: open(os.path.join(fixture_dir, n), encoding="ascii").read() for n in names}
    docs = [(n, sources[n]) for n in names]
    idx = PositionalIndex(docs)

    hits, _ = substring_search(idx, "parse")
    located = []
    for d, start in hits:
        content = docs[d][1]
        line = content.count("\n", 0, start) + 1
        col = start - (content.rfind("\n", 0, start) + 1)
        located.append((docs[d][0], line, col))

    occ = analyze(sources)
    by_pos = {(f, ln, c): (role, s) for f, ln, c, role, s in occ}

    print("== text search for 'parse' through the trigram index")
    print(f"hits: {len(located)} in {len({f for f, _, _ in located})} files")
    for f, ln, c in located:
        kind = token_kind(sources[f], ln, c)
        if kind == "code":
            role, s = by_pos[(f, ln, c)]
            what = f"{role} {s}"
        else:
            what = kind
        print(f"  {f}:{ln}:{c} {what}")

    target = sym("models", "Config#", "parse().")
    refs = [(f, ln, c) for f, ln, c, role, s in occ if s == target and role == "reference"]
    defs = [(f, ln, c) for f, ln, c, role, s in occ if s == target and role == "definition"]
    print()
    print("== semantic find-references")
    print(f"symbol: {target}")
    print(f"definitions: {len(defs)}, references: {len(refs)}")
    for f, ln, c in defs + refs:
        print(f"  {f}:{ln}:{c}")
    noise = len(located) - len(defs) - len(refs)
    print(f"text hits that are not Config.parse: {noise} of {len(located)}")

    # Each count below is fixed by the fixture; if name resolution broke, one of them moves.
    assert len(located) == 11
    assert len(defs) == 1 and len(refs) == 2
    assert {(f, ln) for f, ln, _ in refs} == {("service.py", 12), ("service.py", 19)}
    other = {s for _, _, _, _, s in occ if s.endswith("parse().") and s != target}
    assert other == {sym("models", "Template#", "parse()."), sym("service", "parse().")}


if __name__ == "__main__":
    main(sys.argv[1])
