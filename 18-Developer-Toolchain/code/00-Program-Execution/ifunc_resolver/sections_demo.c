#include <stdio.h>

const char reg_string[] __attribute__((section(".registry"))) = "PLUGIN_REGISTRY_V1";

void dead_code_function(void) {
    printf("This should be eliminated\n");
}

void live_function(void) {
    printf("Live function active\n");
}

int main(void) {
    live_function();
    return 0;
}
