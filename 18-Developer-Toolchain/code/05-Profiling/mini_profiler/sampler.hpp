// In-process sampling front end: ITIMER_PROF -> SIGPROF -> frame-pointer walk.
//
// The handler only copies return addresses into memory that was allocated before the
// timer was armed. Everything that needs malloc, stdio, or the dynamic loader (the
// module table, the output file) happens in stop_and_write(), after the timer is off.
#pragma once

namespace miniprof {

struct Options {
    unsigned interval_us = 1000; // CPU time between two SIGPROF deliveries
    unsigned capacity = 8192;    // samples preallocated before the timer is armed
};

// Installs the SIGPROF handler and arms ITIMER_PROF. Returns false and sets errno on failure;
// errno is EBUSY when the process already uses SIGPROF or ITIMER_PROF.
bool start(const Options& opts);

// Number of samples stored so far. Lock-free, so the workload may poll it.
unsigned samples_taken();

// Disarms the timer, restores the previous SIGPROF action, then writes the raw profile:
// one "map" line per module that a sample hit and one "sample" line of hexadecimal
// addresses per sample, leaf first.
bool stop_and_write(const char* raw_path);

} // namespace miniprof
