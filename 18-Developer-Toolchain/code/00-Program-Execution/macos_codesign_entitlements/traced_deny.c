#include <stdio.h>
#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    pid_t pid = fork();
    if (pid == 0) {
        ptrace(PT_TRACE_ME, 0, 0, 0);
        ptrace(PT_DENY_ATTACH, 0, 0, 0);
        _exit(0);
    }
    int st = 0;
    waitpid(pid, &st, 0);
    if (WIFEXITED(st)) {
        printf("TRACED_DENY_ATTACH: exit=%d\n", WEXITSTATUS(st));
    } else if (WIFSIGNALED(st)) {
        printf("TRACED_DENY_ATTACH: signal=%d\n", WTERMSIG(st));
    } else {
        printf("TRACED_DENY_ATTACH: stopped=%d\n", WSTOPSIG(st));
    }
    return 0;
}
