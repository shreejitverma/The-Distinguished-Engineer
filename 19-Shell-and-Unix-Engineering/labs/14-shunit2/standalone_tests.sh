#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# Tests for standalone mode: this file does not source shUnit2; "sh shunit2 standalone_tests.sh" sources this file.
testOne() { echo 'ran: one'; }
testTwo() { echo 'ran: two'; }
otherFunction() { echo 'ran: other'; }
