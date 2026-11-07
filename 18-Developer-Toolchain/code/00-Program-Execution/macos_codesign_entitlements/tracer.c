#include <mach/mach.h>
#include <mach/vm_map.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 3) {
        return 1;
    }
    pid_t pid = (pid_t)atoi(argv[1]);
    vm_address_t addr = 0;
    if (sscanf(argv[2], "%lx", &addr) != 1) {
        return 1;
    }

    mach_port_t task = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), pid, &task);
    if (kr != KERN_SUCCESS) {
        printf("TASK_FOR_PID: kr=%d (%s)\n", kr, mach_error_string(kr));
        return 0;
    }
    printf("TASK_FOR_PID: kr=0 (%s)\n", mach_error_string(kr));

    unsigned long long token = 0;
    vm_size_t out_size = 0;
    kr = vm_read_overwrite(task, addr, sizeof(token), (vm_address_t)&token, &out_size);
    if (kr == KERN_SUCCESS) {
        printf("VM_READ: token=0x%llX\n", token);
    } else {
        printf("VM_READ: kr=%d\n", kr);
    }
    mach_port_deallocate(mach_task_self(), task);
    return 0;
}
