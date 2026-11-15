/* The classic leak: the preprocessor replaces these with the wall clock unless SOURCE_DATE_EPOCH is set. */
const char *build_stamp(void)
{
    return __DATE__ " " __TIME__;
}
