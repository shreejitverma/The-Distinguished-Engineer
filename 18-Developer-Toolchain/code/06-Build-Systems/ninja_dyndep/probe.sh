#!/bin/sh
# One build edge of the jobserver experiment: mark itself running, sample how many
# edges are running at the same moment, then finish and create its output file.
# Usage: probe.sh OUTPUT RUNDIR SAMPLES
set -eu
out=$1
rundir=$2
samples=$3
: >"$rundir/$out"
sleep 0.3
ls "$rundir" | wc -l | tr -d ' ' >>"$samples"
sleep 0.3
rm -f "$rundir/$out"
: >"$out"
