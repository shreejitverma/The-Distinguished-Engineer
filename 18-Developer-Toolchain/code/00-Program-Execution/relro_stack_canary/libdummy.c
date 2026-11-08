#include <stdio.h>

void target_func(void) {
    printf("Original target_func\n");
}

int target_data = 100;
int *target_data_ptr = &target_data;
