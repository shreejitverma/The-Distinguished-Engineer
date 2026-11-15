/* Counters bumped from several threads with the GIL disabled.
   bump_plain races; bump_atomic and bump_mutex do not.
   bump_turns, hold_then_bump, and block_then_bump fix the schedule, so the lab can
   tell which PyMutex paths ThreadSanitizer can follow in an uninstrumented interpreter. */
#include <Python.h>
#include <sched.h>
#include <unistd.h>

static long plain_counter = 0;
static long atomic_counter = 0;
static long mutex_counter = 0;
static PyMutex counter_mutex = {0};

static int
read_count(PyObject *arg, long *n)
{
    *n = PyLong_AsLong(arg);
    return (*n == -1 && PyErr_Occurred()) ? -1 : 0;
}

static PyObject *
bump_plain(PyObject *self, PyObject *arg)
{
    long n;
    if (read_count(arg, &n) < 0) return NULL;
    for (long i = 0; i < n; i++) {
        plain_counter++;
    }
    Py_RETURN_NONE;
}

static PyObject *
bump_atomic(PyObject *self, PyObject *arg)
{
    long n;
    if (read_count(arg, &n) < 0) return NULL;
    for (long i = 0; i < n; i++) {
        __atomic_fetch_add(&atomic_counter, 1, __ATOMIC_RELAXED);
    }
    Py_RETURN_NONE;
}

static PyObject *
bump_mutex(PyObject *self, PyObject *arg)
{
    long n;
    if (read_count(arg, &n) < 0) return NULL;
    for (long i = 0; i < n; i++) {
        PyMutex_Lock(&counter_mutex);
        mutex_counter++;
        PyMutex_Unlock(&counter_mutex);
    }
    Py_RETURN_NONE;
}

/* The schedule flags are relaxed atomics on purpose: ThreadSanitizer gives a relaxed
   load or store no happens-before edge, so waiting on them fixes the order of the
   increments in time without telling TSan anything about that order. */
static int turn = 0;
static int lock_held = 0;

/* Spin, detached from the interpreter, until *flag == value. */
static void
wait_until(int *flag, int value)
{
    Py_BEGIN_ALLOW_THREADS
    while (__atomic_load_n(flag, __ATOMIC_RELAXED) != value) {
        sched_yield();
    }
    Py_END_ALLOW_THREADS
}

/* bump_turns(me, n, locked): two threads, me = 0 and me = 1, alternate strictly.
   Their increments never overlap in time, and with locked true the mutex is never
   contended, so only the inline fast paths of PyMutex_Lock and PyMutex_Unlock run. */
static PyObject *
bump_turns(PyObject *self, PyObject *args)
{
    int me, locked;
    long n;
    if (!PyArg_ParseTuple(args, "ilp", &me, &n, &locked)) return NULL;
    for (long i = 0; i < n; i++) {
        wait_until(&turn, me);
        if (locked) {
            PyMutex_Lock(&counter_mutex);
            mutex_counter++;
            PyMutex_Unlock(&counter_mutex);
        }
        else {
            plain_counter++;
        }
        __atomic_store_n(&turn, 1 - me, __ATOMIC_RELAXED);
    }
    Py_RETURN_NONE;
}

/* hold_then_bump(settle_us): take the mutex and keep it until the other thread has
   set the "has parked threads" bit, wait settle_us more so that thread is asleep in
   the parking lot, then increment and unlock. Returns whether the bit was set.
   Include/cpython/lock.h documents the bits: 0b01 locked, 0b10 has parked threads. */
static PyObject *
hold_then_bump(PyObject *self, PyObject *arg)
{
    long settle_us;
    int parked;
    if (read_count(arg, &settle_us) < 0) return NULL;
    PyMutex_Lock(&counter_mutex);
    __atomic_store_n(&lock_held, 1, __ATOMIC_RELAXED);
    Py_BEGIN_ALLOW_THREADS
    while ((__atomic_load_n(&counter_mutex._bits, __ATOMIC_RELAXED) & 2) == 0) {
        sched_yield();
    }
    usleep((useconds_t)settle_us);
    Py_END_ALLOW_THREADS
    parked = (__atomic_load_n(&counter_mutex._bits, __ATOMIC_RELAXED) & 2) != 0;
    mutex_counter++;
    PyMutex_Unlock(&counter_mutex);
    return PyBool_FromLong(parked);
}

/* block_then_bump(): block on the held mutex, then increment once it is ours. */
static PyObject *
block_then_bump(PyObject *self, PyObject *unused)
{
    wait_until(&lock_held, 1);
    PyMutex_Lock(&counter_mutex);
    mutex_counter++;
    PyMutex_Unlock(&counter_mutex);
    Py_RETURN_NONE;
}

static PyObject *
counts(PyObject *self, PyObject *unused)
{
    return Py_BuildValue("(lll)", plain_counter,
                         __atomic_load_n(&atomic_counter, __ATOMIC_RELAXED),
                         mutex_counter);
}

static PyMethodDef methods[] = {
    {"bump_plain", bump_plain, METH_O, NULL},
    {"bump_atomic", bump_atomic, METH_O, NULL},
    {"bump_mutex", bump_mutex, METH_O, NULL},
    {"bump_turns", bump_turns, METH_VARARGS, NULL},
    {"hold_then_bump", hold_then_bump, METH_O, NULL},
    {"block_then_bump", block_then_bump, METH_NOARGS, NULL},
    {"counts", counts, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef_Slot slots[] = {
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef def = {
    PyModuleDef_HEAD_INIT, "racy", NULL, 0, methods, slots,
};

PyMODINIT_FUNC
PyInit_racy(void)
{
    return PyModuleDef_Init(&def);
}
