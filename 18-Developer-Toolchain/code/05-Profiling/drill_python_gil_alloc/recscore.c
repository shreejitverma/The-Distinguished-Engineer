/*
 * recscore: score a batch of fixed-size records and return a checksum.
 *
 * One source, five builds, selected with -DVARIANT=<n> and -DMODNAME=<name>:
 *
 *   VARIANT_HOLD   (0)  never releases the GIL around the batch loop
 *   VARIANT_BASE   (1)  release 1.4: releases the GIL for the whole batch
 *   VARIANT_REGR   (2)  release 1.5: per-venue stats and a heap scratch buffer,
 *                       both added inside the loop that runs without the GIL
 *   VARIANT_GILFIX (3)  1.5 with the stats published once per batch
 *   VARIANT_FIXED  (4)  release 1.6: stats once per batch, scratch on the stack
 *
 * The source carries no instrumentation.  run.sh builds a second "probed"
 * flavor of each variant by force-including probe.h, which redirects malloc,
 * free, PyGILState_Ensure, and PyEval_SaveThread to counting wrappers.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define VARIANT_HOLD 0
#define VARIANT_BASE 1
#define VARIANT_REGR 2
#define VARIANT_GILFIX 3
#define VARIANT_FIXED 4

#ifndef VARIANT
#error "build with -DVARIANT=<0..4>"
#endif
#ifndef MODNAME
#error "build with -DMODNAME=<module name>"
#endif

#define REC_SIZE 64
#define NVENUE 8
#define ROUNDS 6

#define STR2(x) #x
#define STR(x) STR2(x)
#define CAT2(a, b) a##b
#define CAT(a, b) CAT2(a, b)

/* Copy a record into scratch space with a cheap byte transform. */
static void normalize(const unsigned char *rec, unsigned char *out) {
    for (int i = 0; i < REC_SIZE; i++) {
        out[i] = (unsigned char)(rec[i] ^ 0x5a);
    }
}

/* FNV-1a over the normalized record, several rounds: a serial dependency
 * chain, so the cost per record is fixed and cannot be vectorized away. */
static uint64_t score(const unsigned char *p) {
    uint64_t h = 1469598103934665603ULL;
    for (int r = 0; r < ROUNDS; r++) {
        for (int i = 0; i < REC_SIZE; i++) {
            h ^= p[i];
            h *= 1099511628211ULL;
        }
    }
    return h;
}

/* stats[key] += n, creating the key on first use.  Needs the GIL. */
static int stats_add(PyObject *stats, int venue, long n) {
    PyObject *key = PyUnicode_FromFormat("venue-%d", venue);
    if (key == NULL) {
        return -1;
    }
    PyObject *old = PyDict_GetItemWithError(stats, key); /* borrowed */
    if (old == NULL && PyErr_Occurred()) {
        Py_DECREF(key);
        return -1;
    }
    long cur = 0;
    if (old != NULL) {
        cur = PyLong_AsLong(old);
        if (cur == -1 && PyErr_Occurred()) {
            Py_DECREF(key);
            return -1;
        }
    }
    PyObject *val = PyLong_FromLong(cur + n);
    if (val == NULL) {
        Py_DECREF(key);
        return -1;
    }
    int rc = PyDict_SetItem(stats, key, val);
    Py_DECREF(val);
    Py_DECREF(key);
    return rc;
}

static PyObject *score_batch(PyObject *self, PyObject *args) {
    Py_buffer view;
    PyObject *stats = Py_None;
    if (!PyArg_ParseTuple(args, "y*|O", &view, &stats)) {
        return NULL;
    }
    if (stats != Py_None && !PyDict_Check(stats)) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_TypeError, "stats must be a dict or None");
        return NULL;
    }
    if (view.len % REC_SIZE != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "buffer length is not a multiple of 64");
        return NULL;
    }
    const unsigned char *data = (const unsigned char *)view.buf;
    Py_ssize_t nrec = view.len / REC_SIZE;
    uint64_t total = 0;
    int failed = 0;

#if VARIANT == VARIANT_HOLD
    /* The whole batch runs with the GIL held: no other Python thread runs. */
    unsigned char tmp[REC_SIZE];
    for (Py_ssize_t i = 0; i < nrec; i++) {
        normalize(data + i * REC_SIZE, tmp);
        total += score(tmp);
    }

#elif VARIANT == VARIANT_BASE
    /* Release 1.4: one GIL release for the batch, no allocation, no stats. */
    Py_BEGIN_ALLOW_THREADS
    unsigned char tmp[REC_SIZE];
    for (Py_ssize_t i = 0; i < nrec; i++) {
        normalize(data + i * REC_SIZE, tmp);
        total += score(tmp);
    }
    Py_END_ALLOW_THREADS

#elif VARIANT == VARIANT_REGR
    /* Release 1.5: "per-venue stats" and "heap scratch for future variable
     * record sizes".  Both landed inside the loop that runs without the GIL. */
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t i = 0; i < nrec && !failed; i++) {
        const unsigned char *rec = data + i * REC_SIZE;
        unsigned char *tmp = malloc(REC_SIZE); /* hidden: libc, once per record */
        if (tmp == NULL) {
            failed = 2;
            break;
        }
        normalize(rec, tmp);
        total += score(tmp);
        free(tmp);
        if (stats != Py_None) {
            /* hidden: the GIL is taken and dropped once per record */
            PyGILState_STATE g = PyGILState_Ensure();
            if (stats_add(stats, rec[0] % NVENUE, 1) < 0 || PyErr_CheckSignals() < 0) {
                failed = 1;
            }
            PyGILState_Release(g);
        }
    }
    Py_END_ALLOW_THREADS

#elif VARIANT == VARIANT_GILFIX
    /* Stats are counted natively and published once; the malloc stays. */
    long counts[NVENUE] = {0};
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t i = 0; i < nrec; i++) {
        const unsigned char *rec = data + i * REC_SIZE;
        unsigned char *tmp = malloc(REC_SIZE);
        if (tmp == NULL) {
            failed = 2;
            break;
        }
        normalize(rec, tmp);
        total += score(tmp);
        free(tmp);
        counts[rec[0] % NVENUE]++;
    }
    Py_END_ALLOW_THREADS
    if (!failed && stats != Py_None) {
        for (int v = 0; v < NVENUE && !failed; v++) {
            if (counts[v] != 0 && stats_add(stats, v, counts[v]) < 0) {
                failed = 1;
            }
        }
        if (!failed && PyErr_CheckSignals() < 0) {
            failed = 1;
        }
    }

#elif VARIANT == VARIANT_FIXED
    /* Release 1.6: stats published once per batch, scratch on the stack. */
    long counts[NVENUE] = {0};
    Py_BEGIN_ALLOW_THREADS
    unsigned char tmp[REC_SIZE];
    for (Py_ssize_t i = 0; i < nrec; i++) {
        const unsigned char *rec = data + i * REC_SIZE;
        normalize(rec, tmp);
        total += score(tmp);
        counts[rec[0] % NVENUE]++;
    }
    Py_END_ALLOW_THREADS
    if (stats != Py_None) {
        for (int v = 0; v < NVENUE && !failed; v++) {
            if (counts[v] != 0 && stats_add(stats, v, counts[v]) < 0) {
                failed = 1;
            }
        }
        if (!failed && PyErr_CheckSignals() < 0) {
            failed = 1;
        }
    }
#else
#error "unknown VARIANT"
#endif

    PyBuffer_Release(&view);
    if (failed == 2) {
        return PyErr_NoMemory();
    }
    if (failed) {
        return NULL;
    }
    return PyLong_FromUnsignedLongLong(total);
}

static PyMethodDef methods[] = {
    {"score_batch", score_batch, METH_VARARGS,
     "score_batch(data, stats=None) -> int: checksum of all 64-byte records in data."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, STR(MODNAME), NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC CAT(PyInit_, MODNAME)(void) { return PyModule_Create(&moduledef); }
