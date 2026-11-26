"""Deterministic synthetic corpus for the trigram lab.

Random code-like files plus planted files that make each index behaviour visible:
a rare identifier, decoys that hold every trigram of a query without holding the query,
a regex target, and vendored byte-for-byte duplicates.
"""

import random

VERBS = ["load", "store", "emit", "merge", "split", "scan", "flush", "drain", "build", "check", "route", "apply"]
NOUNS = ["buffer", "record", "frame", "token", "stream", "batch", "table", "entry", "block", "queue", "index", "value"]


def make_corpus():
    rng = random.Random(20261121)
    files = []
    for i in range(240):
        lines = []
        for _ in range(rng.randint(30, 60)):
            v, a, b, c = rng.choice(VERBS), rng.choice(NOUNS), rng.choice(NOUNS), rng.choice(NOUNS)
            kind = rng.randrange(4)
            if kind == 0:
                lines.append(f"def {v}_{a}({b}):")
            elif kind == 1:
                lines.append(f"    return {v}_{a}({b}, {c})")
            elif kind == 2:
                lines.append(f"    for item in {a}_list:  # {v} each {b}")
            else:
                lines.append(f"    {a}_{b} = {v}_{a}(self.{b})")
        files.append((f"src/pkg{i % 12:02d}/mod{i:03d}.py", "\n".join(lines) + "\n"))

    # Rare identifier: two files hold zxqv_checkpoint_flush, one holds zxqv_checkpoint_drain.
    files.append(("planted/rare_a.py", "def zxqv_checkpoint_flush(self):\n    return 1\n"))
    files.append(("planted/rare_b.py", "x = zxqv_checkpoint_flush()\nclass ZXQV_CheckPoint_FLUSH:\n    pass\n"))
    files.append(("planted/rare_c.py", "y = zxqv_checkpoint_drain()\n"))

    # Three real hits for parse_header( and eight decoys that hold all its trigrams, scattered.
    for k in range(3):
        files.append((f"planted/hdr_hit{k}.py", f"def run():\n    return parse_header(buf{k})\n"))
    for k in range(8):
        files.append((f"planted/hdr_decoy{k}.py", f"a = parse_a({k})\nb = the_head({k})\nc = reader({k})\n"))

    # Regex target: Path or PathFragment, then '=', then /usr/local on the same line.
    for k in range(4):
        files.append((f"planted/path_hit{k}.py", f'Path = "/usr/local/bin{k}"\n'))
    for k in range(2):
        files.append((f"planted/frag_hit{k}.py", f'PathFragment = base{k} + "/usr/local"\n'))
    # Decoys: both pieces present in the file, never on one line in the right order.
    for k in range(5):
        files.append((f"planted/path_decoy{k}.py", f'Path\nx{k} = 1\n"/usr/local"\n'))

    # Vendored duplicates: 30 byte-for-byte copies of 10 base files.
    for k in range(30):
        name, content = files[k % 10]
        files.append((f"vendor/copy{k:02d}/{name.rsplit('/', 1)[1]}", content))
    return files
