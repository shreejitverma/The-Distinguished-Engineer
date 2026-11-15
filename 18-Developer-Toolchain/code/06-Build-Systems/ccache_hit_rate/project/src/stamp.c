/* stamp.c: uses __TIME__, so ccache must not trust a direct-mode hit. */
const char *lab_build_time(void)
{
    return __TIME__;
}
