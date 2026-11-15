#include <stdio.h>
#include "order.h"

int report_version(void);

int main(void) {
    printf("main sees SCHEMA_VERSION %d, report sees %d, sizeof(struct Order) %zu\n",
           SCHEMA_VERSION, report_version(), sizeof(struct Order));
    return 0;
}
