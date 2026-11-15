/* A multi-phase extension with no Py_mod_gil slot.
   Importing it into a free-threaded interpreter re-enables the GIL. */
#include <Python.h>

static struct PyModuleDef_Slot slots[] = {
    {0, NULL},
};

static struct PyModuleDef def = {
    PyModuleDef_HEAD_INIT, "legacy", NULL, 0, NULL, slots,
};

PyMODINIT_FUNC
PyInit_legacy(void)
{
    return PyModuleDef_Init(&def);
}
