/*
 * probe.h: counting wrappers for a "probed" build of recscore.c.
 *
 * build.sh force-includes this header (-include probe.h) for the probed
 * flavor only.  It redirects four calls made by the extension source to
 * wrappers that bump an atomic counter and then call the real function:
 *
 *   malloc, free                       libc allocation made by the extension
 *   PyGILState_Ensure                  GIL (re)acquisition from a no-GIL region
 *   PyEval_SaveThread                  GIL release (Py_BEGIN_ALLOW_THREADS)
 *
 * The counters live in the probed .so and are read with ctypes through
 * recscore_probe_read().  This is the build-time stand-in for what a uprobe,
 * ltrace, or memray's symbol patching counts in production; the timed phases
 * of the lab never use the probed flavor.
 */
#ifndef RECSCORE_PROBE_H
#define RECSCORE_PROBE_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdatomic.h>
#include <stdlib.h>

static atomic_long probe_n_malloc;
static atomic_long probe_n_free;
static atomic_long probe_n_ensure;
static atomic_long probe_n_save;

static inline void *probe_malloc(size_t n) {
    atomic_fetch_add_explicit(&probe_n_malloc, 1, memory_order_relaxed);
    return malloc(n);
}

static inline void probe_free(void *p) {
    atomic_fetch_add_explicit(&probe_n_free, 1, memory_order_relaxed);
    free(p);
}

static inline PyGILState_STATE probe_gilstate_ensure(void) {
    atomic_fetch_add_explicit(&probe_n_ensure, 1, memory_order_relaxed);
    return PyGILState_Ensure();
}

static inline PyThreadState *probe_save_thread(void) {
    atomic_fetch_add_explicit(&probe_n_save, 1, memory_order_relaxed);
    return PyEval_SaveThread();
}

/* out[0..3] = malloc, free, PyGILState_Ensure, PyEval_SaveThread; then reset. */
__attribute__((visibility("default"))) void recscore_probe_read(long *out) {
    out[0] = atomic_exchange(&probe_n_malloc, 0);
    out[1] = atomic_exchange(&probe_n_free, 0);
    out[2] = atomic_exchange(&probe_n_ensure, 0);
    out[3] = atomic_exchange(&probe_n_save, 0);
}

#define malloc(n) probe_malloc(n)
#define free(p) probe_free(p)
#define PyGILState_Ensure() probe_gilstate_ensure()
#define PyEval_SaveThread() probe_save_thread()

#endif /* RECSCORE_PROBE_H */
