#!/bin/sh
# stdin-reader.sh: a script whose second command reads standard input.
# Piped into a shell, the script text and the data share one descriptor.
# POSIX (XCU sh, STDIN) says the shell must not read ahead, so `read` takes line 2 as data;
# a shell that reads ahead has already consumed line 2 and runs it as a command instead.
read -r line
echo executed-by-the-shell
echo "line=[$line]"
