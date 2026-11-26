# shellcheck shell=bash
setup_suite() { echo suite-setup >>"$TRACE"; }
teardown_suite() { echo suite-teardown >>"$TRACE"; }
