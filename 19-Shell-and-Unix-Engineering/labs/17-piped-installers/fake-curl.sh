#!/bin/sh
# fake-curl.sh: stand-in for `curl URL` so the lab needs no network.
# Usage: fake-curl.sh ok FILE         print FILE, exit 0 (a complete download)
#        fake-curl.sh cut FILE N      print the first N bytes of FILE, exit 18 (curl's CURLE_PARTIAL_FILE)
#        fake-curl.sh fail            print nothing, exit 22 (curl --fail on an HTTP error)
set -u
case ${1-} in
  ok)
    exec cat "$2"
    ;;
  cut)
    head -c "$3" "$2"
    exit 18
    ;;
  fail)
    exit 22
    ;;
  *)
    echo "usage: fake-curl.sh ok FILE | cut FILE N | fail" >&2
    exit 64
    ;;
esac
