#!/usr/bin/env python3
"""Strip '#' comment lines and trailing blanks; runs one-shot or as a JSON persistent worker."""

import json
import sys


def expand(argv):
    """Expand @flagfile arguments, one argument per line (Bazel's multiline format)."""
    out = []
    for arg in argv:
        if arg.startswith("@") and not arg.startswith("@@"):
            with open(arg[1:], encoding="utf-8") as fh:
                out.extend(line.rstrip("\n") for line in fh if line.strip())
        else:
            out.append(arg)
    return out


def normalize(argv):
    opts = dict(zip(argv[0::2], argv[1::2]))
    with open(opts["--src"], encoding="utf-8") as fh:
        lines = [ln.rstrip() for ln in fh if not ln.lstrip().startswith("#")]
    with open(opts["--out"], "w", encoding="utf-8") as fh:
        fh.write("\n".join(ln for ln in lines if ln) + "\n")


def serve():
    served = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        code, output = 0, ""
        try:
            normalize(expand(request.get("arguments", [])))
        except Exception as exc:  # report through the protocol, never on stdout
            code, output = 1, "normalize failed: %s" % exc
        served += 1
        # stderr goes to the per-worker log under <output_base>/bazel-workers.
        sys.stderr.write("normalize-worker served request %d\n" % served)
        sys.stderr.flush()
        response = {"exitCode": code, "output": output, "requestId": request.get("requestId", 0)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if "--persistent_worker" in sys.argv[1:]:
        serve()
    else:
        normalize(expand(sys.argv[1:]))
