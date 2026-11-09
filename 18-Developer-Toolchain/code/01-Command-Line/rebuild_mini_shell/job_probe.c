/* Test child for mini_shell.
 *
 * Prints one PROBE line on stderr describing its process group and the terminal foreground group,
 * then handles its arguments in order:
 *   --raw        clear ECHO on the terminal and keep ICANON (a stand-in for a program with its own terminal modes)
 *   --stop       raise SIGTSTP on itself, then report the ECHO and ICANON state it sees after resuming
 *   --spin[=MS]  sleep for MS milliseconds (default 300) and report completion
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static void sleep_ms(long ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {
    }
}

static const char *flag_state(int fd, tcflag_t flag) {
    struct termios t;
    if (tcgetattr(fd, &t) != 0) return "unknown";
    return (t.c_lflag & flag) ? "on" : "off";
}

int main(int argc, char **argv) {
    pid_t pid = getpid();
    pid_t pgid = getpgrp();

    int tty_fd = isatty(STDIN_FILENO) ? STDIN_FILENO : STDERR_FILENO;
    pid_t fg = tcgetpgrp(tty_fd);

    fprintf(stderr, "PROBE: pid=%d pgid=%d fg=%d is_fg=%s\n",
            (int)pid,
            (int)pgid,
            (int)fg,
            (pgid == fg) ? "yes" : "no");
    fflush(stderr);

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--raw") == 0) {
            struct termios t;
            if (tcgetattr(tty_fd, &t) == 0) {
                t.c_lflag &= ~(tcflag_t)ECHO;
                tcsetattr(tty_fd, TCSANOW, &t);
            }
        } else if (strcmp(argv[i], "--stop") == 0) {
            raise(SIGTSTP);
            fprintf(stderr, "PROBE: resumed after SIGTSTP echo=%s icanon=%s\n",
                    flag_state(tty_fd, ECHO), flag_state(tty_fd, ICANON));
            fflush(stderr);
        } else if (strncmp(argv[i], "--spin", 6) == 0) {
            long ms = 300;
            if (argv[i][6] == '=') ms = atol(argv[i] + 7);
            sleep_ms(ms);
            fprintf(stderr, "PROBE: finished spin\n");
            fflush(stderr);
        }
    }

    return 0;
}
