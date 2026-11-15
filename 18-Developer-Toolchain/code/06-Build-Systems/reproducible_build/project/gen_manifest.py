"""Code generator run by the build: writes manifest.h to stdout.

It carries two classic generator bugs on purpose:
- it iterates a set, whose order depends on PYTHONHASHSEED for str elements;
- it formats a timestamp in local time, so TZ leaks into the output.
REPRO_FIX_SORT=1 switches on the code fix for the first bug.
The second is fixed from outside by setting TZ=UTC0 in the build environment.
"""

import os
import sys
import time

FEATURES = {"simd", "tls", "zstd", "numa", "huge-pages", "io-uring", "lto", "pgo"}


def main() -> None:
    names = sorted(FEATURES) if os.environ.get("REPRO_FIX_SORT") == "1" else list(FEATURES)
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = int(epoch) if epoch is not None else int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(when))
    sys.stdout.write("#define FEATURES \"%s\"\n" % ",".join(names))
    sys.stdout.write("#define GENERATED_ON \"%s\"\n" % stamp)


if __name__ == "__main__":
    main()
