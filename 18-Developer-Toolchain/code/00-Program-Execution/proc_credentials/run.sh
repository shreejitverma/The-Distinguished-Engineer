#!/usr/bin/env bash
# requires: linux
set -euo pipefail

CC="${CC:-gcc}"
"$CC" -std=c11 -Wall -Wextra -O2 -o lab main.c
./lab
