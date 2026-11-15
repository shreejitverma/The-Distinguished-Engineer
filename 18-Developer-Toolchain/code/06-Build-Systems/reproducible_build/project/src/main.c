#include <assert.h>
#include <stdio.h>

#include "manifest.h"

const char *build_stamp(void);
int seed_value(void);
int table_value(void);
int zone_value(void);

int main(void)
{
    /* assert() expands __FILE__, so a release build with NDEBUG unset embeds the source path. */
    assert(seed_value() + table_value() + zone_value() == 6);
    printf("stamp: %s\n", build_stamp());
    printf("file: %s\n", __FILE__);
    printf("features: %s\n", FEATURES);
    printf("generated: %s\n", GENERATED_ON);
    return 0;
}
