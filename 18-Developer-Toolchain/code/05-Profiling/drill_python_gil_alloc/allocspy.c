/*
 * allocspy: count calls into CPython's three allocator domains.
 *
 * start() wraps PYMEM_DOMAIN_RAW, PYMEM_DOMAIN_MEM, and PYMEM_DOMAIN_OBJ with
 * PyMem_SetAllocator(), the same public hook that tracemalloc and
 * `memray run --trace-python-allocators` use.  stop() restores the original
 * allocators and returns {"raw"|"mem"|"obj": (malloc, calloc, realloc, free)}.
 *
 * Unlike a tracemalloc snapshot, the counters are cumulative, so an object
 * that is allocated and freed inside one C call still counts.  libc malloc
 * called directly by an extension never reaches these hooks.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdatomic.h>

typedef struct {
    PyMemAllocatorEx orig;
    atomic_long n_malloc;
    atomic_long n_calloc;
    atomic_long n_realloc;
    atomic_long n_free;
} domain_t;

static domain_t domains[3];
static const PyMemAllocatorDomain domain_ids[3] = {
    PYMEM_DOMAIN_RAW, PYMEM_DOMAIN_MEM, PYMEM_DOMAIN_OBJ};
static const char *const domain_names[3] = {"raw", "mem", "obj"};
static int installed;

static void *hook_malloc(void *ctx, size_t size) {
    domain_t *d = (domain_t *)ctx;
    atomic_fetch_add_explicit(&d->n_malloc, 1, memory_order_relaxed);
    return d->orig.malloc(d->orig.ctx, size);
}

static void *hook_calloc(void *ctx, size_t nelem, size_t elsize) {
    domain_t *d = (domain_t *)ctx;
    atomic_fetch_add_explicit(&d->n_calloc, 1, memory_order_relaxed);
    return d->orig.calloc(d->orig.ctx, nelem, elsize);
}

static void *hook_realloc(void *ctx, void *ptr, size_t size) {
    domain_t *d = (domain_t *)ctx;
    atomic_fetch_add_explicit(&d->n_realloc, 1, memory_order_relaxed);
    return d->orig.realloc(d->orig.ctx, ptr, size);
}

static void hook_free(void *ctx, void *ptr) {
    domain_t *d = (domain_t *)ctx;
    atomic_fetch_add_explicit(&d->n_free, 1, memory_order_relaxed);
    d->orig.free(d->orig.ctx, ptr);
}

static PyObject *spy_start(PyObject *self, PyObject *noargs) {
    if (installed) {
        PyErr_SetString(PyExc_RuntimeError, "allocspy is already started");
        return NULL;
    }
    for (int i = 0; i < 3; i++) {
        PyMem_GetAllocator(domain_ids[i], &domains[i].orig);
        atomic_store(&domains[i].n_malloc, 0);
        atomic_store(&domains[i].n_calloc, 0);
        atomic_store(&domains[i].n_realloc, 0);
        atomic_store(&domains[i].n_free, 0);
        PyMemAllocatorEx hook = {&domains[i], hook_malloc, hook_calloc, hook_realloc, hook_free};
        PyMem_SetAllocator(domain_ids[i], &hook);
    }
    installed = 1;
    Py_RETURN_NONE;
}

static PyObject *spy_stop(PyObject *self, PyObject *noargs) {
    if (!installed) {
        PyErr_SetString(PyExc_RuntimeError, "allocspy is not started");
        return NULL;
    }
    long counts[3][4];
    for (int i = 0; i < 3; i++) {
        PyMem_SetAllocator(domain_ids[i], &domains[i].orig);
        counts[i][0] = atomic_load(&domains[i].n_malloc);
        counts[i][1] = atomic_load(&domains[i].n_calloc);
        counts[i][2] = atomic_load(&domains[i].n_realloc);
        counts[i][3] = atomic_load(&domains[i].n_free);
    }
    installed = 0;
    PyObject *result = PyDict_New();
    if (result == NULL) {
        return NULL;
    }
    for (int i = 0; i < 3; i++) {
        PyObject *t = Py_BuildValue("(llll)", counts[i][0], counts[i][1], counts[i][2], counts[i][3]);
        if (t == NULL || PyDict_SetItemString(result, domain_names[i], t) < 0) {
            Py_XDECREF(t);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(t);
    }
    return result;
}

static PyMethodDef methods[] = {
    {"start", spy_start, METH_NOARGS, "Install counting hooks on the three allocator domains."},
    {"stop", spy_stop, METH_NOARGS, "Remove the hooks and return the counts."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "allocspy", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit_allocspy(void) { return PyModule_Create(&moduledef); }
