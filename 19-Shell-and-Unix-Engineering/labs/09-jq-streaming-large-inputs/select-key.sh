#!/bin/sh
# select-key.sh FILE KEY: print the stream events whose top-level key is exactly KEY.
# KEY travels through --arg, so quotes, dollar signs and jq syntax in it stay data.
exec jq -c --stream --arg k "$2" 'select(length == 2 and .[0][0] == $k)' -- "$1"
