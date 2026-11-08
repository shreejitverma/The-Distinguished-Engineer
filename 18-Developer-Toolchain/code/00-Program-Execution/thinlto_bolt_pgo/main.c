#include <stdio.h>
#include <stdlib.h>

extern int packet_process(int type, int payload);

int main(int argc, char **argv) {
    int count = 50000;
    long long checksum = 0;
    for (int i = 0; i < count; i++) {
        int type = (i % 1000 == 0) ? 2 : 1;
        checksum += packet_process(type, i);
    }
    printf("Checksum: %lld\n", checksum);
    return 0;
}
