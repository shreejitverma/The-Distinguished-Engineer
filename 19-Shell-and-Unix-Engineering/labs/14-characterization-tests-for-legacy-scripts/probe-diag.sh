#!/bin/sh
# probe-diag.sh - call a command that does not exist and report the status the shell gives.
nosuchcmd-xyz
echo "rc=$?"
