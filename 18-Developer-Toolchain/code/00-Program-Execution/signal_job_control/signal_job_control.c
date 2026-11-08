#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

/* Check 1 state: Standard vs Real-time signal delivery */
static volatile sig_atomic_t g_usr1_count = 0;
static volatile sig_atomic_t g_rt_count = 0;
static volatile int g_rt_payloads[8];

static void usr1_handler(int sig) {
    (void)sig;
    g_usr1_count++;
}

static void rt_handler(int sig, siginfo_t *info, void *ucontext) {
    (void)sig;
    (void)ucontext;
    if (g_rt_count < 8) {
        g_rt_payloads[g_rt_count] = info->si_value.sival_int;
    }
    g_rt_count++;
}

/* Check 2 state: Alternate signal stack */
static volatile bool g_altstack_active = false;
static volatile bool g_frame_in_window = false;
static void *g_altstack_base = NULL;
static size_t g_altstack_size = 0;

static void usr2_handler(int sig, siginfo_t *info, void *ucontext) {
    (void)sig;
    (void)info;
    (void)ucontext;
    stack_t cur_oss;
    if (sigaltstack(NULL, &cur_oss) == 0) {
        if (cur_oss.ss_flags & SS_ONSTACK) {
            g_altstack_active = true;
        }
    }
    int frame_var = 42;
    void *frame_addr = (void *)&frame_var;
    if (frame_addr >= g_altstack_base &&
        frame_addr < (void *)((char *)g_altstack_base + g_altstack_size)) {
        g_frame_in_window = true;
    }
}

/* Check 5B state: Orphaned stopped group signal handling */
static volatile sig_atomic_t g_orphan_got_hup = 0;
static volatile sig_atomic_t g_orphan_got_cont = 0;

static void orphan_hup(int sig) {
    (void)sig;
    g_orphan_got_hup = 1;
}

static void orphan_cont(int sig) {
    (void)sig;
    g_orphan_got_cont = 1;
}

/* Failsafe: a broken mechanism must fail the lab, not hang it.
 * Needs a handler because the lab may run as PID 1 in a container, where a default-action SIGALRM is ignored. */
static void on_alarm(int sig) {
    (void)sig;
    static const char msg[] = "lab timeout\n";
    ssize_t nw = write(STDERR_FILENO, msg, sizeof(msg) - 1);
    (void)nw;
    _exit(2);
}

int main(void) {
    setlinebuf(stdout);
    signal(SIGALRM, on_alarm);
    alarm(30);

    /* --- CHECK 1: Signal Queuing and Coalescing --- */
    printf("[CHECK 1: SIGNAL QUEUE]\n");
    struct sigaction sa1;
    memset(&sa1, 0, sizeof(sa1));
    sa1.sa_handler = usr1_handler;
    sigaction(SIGUSR1, &sa1, NULL);

    struct sigaction sa_rt;
    memset(&sa_rt, 0, sizeof(sa_rt));
    sa_rt.sa_sigaction = rt_handler;
    sa_rt.sa_flags = SA_SIGINFO;
    sigaction(SIGRTMIN, &sa_rt, NULL);

    sigset_t block_mask, old_mask;
    sigemptyset(&block_mask);
    sigaddset(&block_mask, SIGUSR1);
    sigaddset(&block_mask, SIGRTMIN);
    sigprocmask(SIG_BLOCK, &block_mask, &old_mask);

    /* Send three standard signals while blocked */
    kill(getpid(), SIGUSR1);
    kill(getpid(), SIGUSR1);
    kill(getpid(), SIGUSR1);

    /* Send three real-time signals with payloads */
    union sigval sv1, sv2, sv3;
    sv1.sival_int = 100;
    sv2.sival_int = 101;
    sv3.sival_int = 102;
    sigqueue(getpid(), SIGRTMIN, sv1);
    sigqueue(getpid(), SIGRTMIN, sv2);
    sigqueue(getpid(), SIGRTMIN, sv3);

    /* Unblock signals to trigger delivery */
    sigprocmask(SIG_SETMASK, &old_mask, NULL);

    printf("Standard signal SIGUSR1 instances received: %d (coalesced)\n", g_usr1_count);
    printf("Real-time signal SIGRTMIN instances received: %d (queued)\n", g_rt_count);
    printf("Real-time payloads: %d %d %d\n", g_rt_payloads[0], g_rt_payloads[1], g_rt_payloads[2]);

    /* --- CHECK 2: Alternate Signal Stack (sigaltstack) --- */
    printf("\n[CHECK 2: SIGALTSTACK]\n");
    g_altstack_size = 65536;
    g_altstack_base = mmap(NULL, g_altstack_size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    stack_t ss;
    ss.ss_sp = g_altstack_base;
    ss.ss_size = g_altstack_size;
    ss.ss_flags = 0;
    sigaltstack(&ss, NULL);

    struct sigaction sa2;
    memset(&sa2, 0, sizeof(sa2));
    sa2.sa_sigaction = usr2_handler;
    sa2.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigaction(SIGUSR2, &sa2, NULL);

    raise(SIGUSR2);

    printf("Alternate stack active in handler: %s\n", g_altstack_active ? "true" : "false");
    printf("Handler frame within sigaltstack memory window: %s\n", g_frame_in_window ? "true" : "false");

    stack_t disable_ss;
    memset(&disable_ss, 0, sizeof(disable_ss));
    disable_ss.ss_flags = SS_DISABLE;
    sigaltstack(&disable_ss, NULL);
    munmap(g_altstack_base, g_altstack_size);

    /* --- PTY Setup for Job Control Checks --- */
    int pty_master = posix_openpt(O_RDWR | O_NOCTTY);
    if (pty_master < 0) {
        perror("posix_openpt");
        return 1;
    }
    if (grantpt(pty_master) < 0 || unlockpt(pty_master) < 0) {
        perror("pty setup");
        close(pty_master);
        return 1;
    }
    char *slave_name = ptsname(pty_master);

    /* --- CHECK 3: Job Control SIGTTIN --- */
    printf("\n[CHECK 3: JOB CONTROL SIGTTIN]\n");
    pid_t sess_pid3 = fork();
    if (sess_pid3 == 0) {
        setsid();
        int slave = open(slave_name, O_RDWR);
        ioctl(slave, TIOCSCTTY, 0);
        tcsetpgrp(slave, getpid());

        pid_t bg_child = fork();
        if (bg_child == 0) {
            setpgid(0, 0);
            char b[16];
            ssize_t nr = read(slave, b, sizeof(b));
            (void)nr;
            _exit(0);
        }
        setpgid(bg_child, bg_child);
        int st = 0;
        waitpid(bg_child, &st, WUNTRACED);
        if (WIFSTOPPED(st)) {
            printf("Background process reading from controlling terminal stopped: true\n");
            printf("Stop signal: SIGTTIN (%d)\n", WSTOPSIG(st));
        }
        kill(bg_child, SIGKILL);
        waitpid(bg_child, NULL, 0);
        close(slave);
        _exit(0);
    }
    waitpid(sess_pid3, NULL, 0);

    /* --- CHECK 4: Job Control SIGTTOU --- */
    printf("\n[CHECK 4: JOB CONTROL SIGTTOU]\n");
    pid_t sess_pid4 = fork();
    if (sess_pid4 == 0) {
        setsid();
        int slave = open(slave_name, O_RDWR);
        ioctl(slave, TIOCSCTTY, 0);
        tcsetpgrp(slave, getpid());

        struct termios tio;
        tcgetattr(slave, &tio);
        tio.c_lflag |= TOSTOP;
        tcsetattr(slave, TCSANOW, &tio);

        pid_t bg_child = fork();
        if (bg_child == 0) {
            setpgid(0, 0);
            ssize_t nw = write(slave, "v", 1);
            (void)nw;
            _exit(0);
        }
        setpgid(bg_child, bg_child);
        int st = 0;
        waitpid(bg_child, &st, WUNTRACED);
        if (WIFSTOPPED(st)) {
            printf("Background process writing to TOSTOP terminal stopped: true\n");
            printf("Stop signal: SIGTTOU (%d)\n", WSTOPSIG(st));
        }
        kill(bg_child, SIGKILL);
        waitpid(bg_child, NULL, 0);
        close(slave);
        _exit(0);
    }
    waitpid(sess_pid4, NULL, 0);

    /* --- CHECK 5: Orphaned Process Group Protection --- */
    printf("\n[CHECK 5: ORPHANED PROCESS GROUP]\n");
    int pipe_sync[2];
    int pipe_res[2];
    int pipe_done[2];
    if (pipe(pipe_sync) < 0 || pipe(pipe_res) < 0 || pipe(pipe_done) < 0) {
        perror("pipe");
        return 1;
    }

    pid_t sess_pid5 = fork();
    if (sess_pid5 == 0) {
        setsid();
        int slave = open(slave_name, O_RDWR);
        ioctl(slave, TIOCSCTTY, 0);
        tcsetpgrp(slave, getpid());

        pid_t inter = fork();
        if (inter == 0) {
            pid_t gchild = fork();
            if (gchild == 0) {
                setpgid(0, 0);
                char tok;
                ssize_t ntok = read(pipe_sync[0], &tok, 1);
                (void)ntok;
                char buf[16];
                errno = 0;
                ssize_t n = read(slave, buf, sizeof(buf));
                (void)n;
                dprintf(pipe_res[1], "Orphaned background read errno: %s (%d)\n",
                        errno == EIO ? "EIO" : "OTHER", errno);
                close(slave);
                _exit(0); /* closing pipe_done[1] tells the session leader the read finished */
            }
            setpgid(gchild, gchild);
            _exit(0);
        }
        /* Only gchild may still hold the write ends of pipe_res and pipe_done. */
        close(pipe_res[1]);
        close(pipe_done[1]);
        waitpid(inter, NULL, 0);
        ssize_t nsync = write(pipe_sync[1], "k", 1);
        (void)nsync;
        /* Keep the session (and its controlling terminal) alive until gchild exits: EOF on pipe_done. */
        char done;
        ssize_t ndone = read(pipe_done[0], &done, 1);
        (void)ndone;
        close(slave);
        _exit(0);
    }
    close(pipe_res[1]);
    close(pipe_done[0]);
    close(pipe_done[1]);
    char buf_res[128];
    ssize_t nres = read(pipe_res[0], buf_res, sizeof(buf_res) - 1);
    if (nres > 0) {
        buf_res[nres] = '\0';
        printf("%s", buf_res);
    }
    waitpid(sess_pid5, NULL, 0);

    /* Check 5B: Stopped process in orphaned group receiving SIGHUP + SIGCONT */
    int pipe_hup_res[2];
    if (pipe(pipe_hup_res) < 0) {
        perror("pipe");
        return 1;
    }

    pid_t sess_pid5b = fork();
    if (sess_pid5b == 0) {
        setsid();
        pid_t stop_child = fork();
        if (stop_child == 0) {
            setpgid(0, 0);
            signal(SIGHUP, orphan_hup);
            signal(SIGCONT, orphan_cont);
            raise(SIGSTOP);
            dprintf(pipe_hup_res[1], "Orphaned stopped process received SIGHUP: %s\n"
                                     "Orphaned stopped process received SIGCONT: %s\n",
                    g_orphan_got_hup ? "true" : "false",
                    g_orphan_got_cont ? "true" : "false");
            _exit(0);
        }
        setpgid(stop_child, stop_child);
        int st = 0;
        waitpid(stop_child, &st, WUNTRACED);
        _exit(0); /* Exiting orphans stop_child while it is stopped */
    }
    close(pipe_hup_res[1]);
    waitpid(sess_pid5b, NULL, 0);
    char buf_hup[128];
    ssize_t nhup = read(pipe_hup_res[0], buf_hup, sizeof(buf_hup) - 1);
    if (nhup > 0) {
        buf_hup[nhup] = '\0';
        printf("%s", buf_hup);
    }

    close(pty_master);
    return 0;
}
