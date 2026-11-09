#!/usr/bin/env python3
"""Write five records, one every 100 ms, through Python's own sys.stdout (no explicit flush)."""
import sys
import time

for i in range(1, 6):
    sys.stdout.write(f"val {i}\n")
    time.sleep(0.1)
