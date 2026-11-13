Report fixtures for analyze.py's self-test.

Each *.c2c.txt is the unmodified stdout of
    perf c2c report -i <variant>.data --stdio -c tid,iaddr
from perf 6.8.12 (Ubuntu 24.04 linux-tools 6.8.0-149) on linux-aarch64.

The inputs were not recorded on hardware: make_perf_data.py writes a perf.data file with
hand-made memory samples that model each variant of ingest.c (same symbols, thread names,
offsets, and line addresses for a non-randomized load at 0x555555554000). The report layout
is therefore real, and every count in it is invented. Code addresses are arbitrary offsets
inside ingest and main, so the Source:Line column is only roughly right.

  array.c2c.txt        packed shards, HITM loads: false sharing
  array-peer.c2c.txt   the same samples as an Arm SPE host reports them (peer snoop, ARCH aarch64)
  array-fixed.c2c.txt  no HITM at all: the report lists no shared line
  heap.c2c.txt         shards 32 bytes apart inside [heap]: false sharing
  total.c2c.txt        four threads on one offset: true sharing

To regenerate after changing ingest.c (line numbers and symbol addresses move):
    cc -O2 -g -pthread -DFS_LINE=64 ingest.c -o ingest
    python3 fixtures/make_perf_data.py ./ingest array > array.data
    perf c2c report -i array.data --stdio -c tid,iaddr > fixtures/array.c2c.txt
and likewise for array-fixed, heap, total, and `array aarch64` for array-peer.
The expected line addresses in analyze.py's selftest() must match the regenerated reports.
