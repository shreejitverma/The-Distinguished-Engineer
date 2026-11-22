#!/bin/sh
# installer-heredoc.sh: an installer that writes its config file through a here-document.
# Usage: CONF_OUT=OUTFILE installer-heredoc.sh
# The path comes from the environment, not an argument, so any shell can run it from a pipe (posh has no -s).
# POSIX XCU 2.7.4 lets a shell treat a here-document that hits end of input without its delimiter
# as an error, but does not require it, so a truncated download can write a short file.
cat >"$CONF_OUT" <<'EOF_CONFIG'
listen = 127.0.0.1:8080
data_dir = /var/lib/tool
log_level = info
EOF_CONFIG
echo "config written"
