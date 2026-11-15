/* The same module, but it declares that it can run without the GIL. */
#include <Python.h>

static struct PyModuleDef_Slot slots[] = {
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};

static struct PyModuleDef def = {
    PyModuleDef_HEAD_INIT, "ready", NULL, 0, NULL, slots,
};

PyMODINIT_FUNC
PyInit_ready(void)
{
    return PyModuleDef_Init(&def);
}
