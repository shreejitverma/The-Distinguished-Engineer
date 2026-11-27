#include <stdio.h>

volatile int global_counter = 0;

void target_function(int a, int b) {
    int sum = a + b;
    global_counter += sum;
}

int main(void) {
    target_function(10, 20);
    target_function(30, 40);
    return 0;
}
