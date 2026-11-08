#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t g_child_sigint = 0;
static volatile sig_atomic_t g_child_sigwinch = 0;

static void handle_sigint(int sig) {
    (void)sig;
    g_child_sigint = 1;
}

static void handle_sigwinch(int sig) {
    (void)sig;
    g_child_sigwinch = 1;
}

static int open_pty_pair(int *master_fd, int *slave_fd) {
    int m = posix_openpt(O_RDWR | O_NOCTTY | O_CLOEXEC);
    if (m < 0) {
        perror("posix_openpt");
        return -1;
    }
    if (grantpt(m) < 0) {
        perror("grantpt");
        close(m);
        return -1;
    }
    if (unlockpt(m) < 0) {
        perror("unlockpt");
        close(m);
        return -1;
    }

    /* Pathless slave open (Linux 4.13+); no ptsname_r fallback, so a regression fails the lab. */
    int s = ioctl(m, TIOCGPTPEER, O_RDWR | O_NOCTTY);
    if (s < 0) {
        perror("ioctl TIOCGPTPEER");
        close(m);
        return -1;
    }

    *master_fd = m;
    *slave_fd = s;
    return 0;
}

int main(void) {
    /* Hang guard: a broken mechanism blocks a read forever, so turn that into a failure. */
    alarm(30);
    int m, s;
    if (open_pty_pair(&m, &s) < 0) {
        return 1;
    }
    printf("[1] PTY allocation: master fd allocated, slave unlocked and connected\n");

    /* Step 2: Check baseline termios */
    struct termios orig;
    if (tcgetattr(s, &orig) < 0) {
        perror("tcgetattr");
        return 1;
    }
    int has_icanon = (orig.c_lflag & ICANON) ? 1 : 0;
    int has_echo   = (orig.c_lflag & ECHO) ? 1 : 0;
    int has_isig   = (orig.c_lflag & ISIG) ? 1 : 0;
    int has_opost  = (orig.c_oflag & OPOST) ? 1 : 0;
    int has_onlcr  = (orig.c_oflag & ONLCR) ? 1 : 0;
    int has_icrnl  = (orig.c_iflag & ICRNL) ? 1 : 0;
    printf("[2] Baseline attributes: ICANON=%d ECHO=%d ISIG=%d OPOST=%d ONLCR=%d ICRNL=%d\n",
           has_icanon, has_echo, has_isig, has_opost, has_onlcr, has_icrnl);

    /* Step 3: Canonical mode line editing (ERASE 0x7f and WERASE 0x17) */
    struct termios edit_tio = orig;
    edit_tio.c_lflag &= ~ECHO; /* Turn off echo so echoed characters do not buffer in master */
    if (tcsetattr(s, TCSANOW, &edit_tio) < 0) {
        perror("tcsetattr edit");
        return 1;
    }

    /* "helxo" + 2 DELs -> "hel" + "lo worl" -> "hello worl" + WERASE (^W) -> "hello " + "planet\n" */
    const char edit_input[] = "helxo\x7f\x7flo worl\x17planet\n";
    if (write(m, edit_input, sizeof(edit_input) - 1) < 0) {
        perror("write edit");
        return 1;
    }

    char line_buf[64];
    memset(line_buf, 0, sizeof(line_buf));
    ssize_t n_read = read(s, line_buf, sizeof(line_buf) - 1);
    if (n_read < 0) {
        perror("read canonical edit");
        return 1;
    }
    /* Strip trailing newline for clean display */
    if (n_read > 0 && line_buf[n_read - 1] == '\n') {
        line_buf[n_read - 1] = '\0';
    }
    printf("[3] Canonical editing: sent %zu raw bytes, line discipline delivered %zd bytes: \"%s\"\n",
           sizeof(edit_input) - 1, n_read, line_buf);

    /* Step 4: Canonical EOF (VEOF, 0x04) */
    /* Mid-line EOF flushes partial line without newline */
    const char partial[] = "interrupted_payload";
    if (write(m, partial, strlen(partial)) < 0) {
        perror("write partial");
        return 1;
    }
    char veof = orig.c_cc[VEOF]; /* 0x04 */
    if (write(m, &veof, 1) < 0) {
        perror("write veof");
        return 1;
    }
    memset(line_buf, 0, sizeof(line_buf));
    ssize_t n_partial = read(s, line_buf, sizeof(line_buf) - 1);

    /* Empty-line EOF returns 0 bytes */
    if (write(m, &veof, 1) < 0) {
        perror("write veof empty");
        return 1;
    }
    ssize_t n_eof = read(s, line_buf, sizeof(line_buf) - 1);
    printf("[4] Canonical EOF: mid-line VEOF delivered %zd bytes (\"%s\"); empty-line VEOF returned %zd bytes\n",
           n_partial, line_buf, n_eof);

    /* Step 5: Output post-processing and staircase effect */
    /* When OPOST and ONLCR are active */
    struct termios post_tio = orig;
    post_tio.c_oflag |= (OPOST | ONLCR);
    tcsetattr(s, TCSANOW, &post_tio);
    if (write(s, "line\n", 5) < 0) {
        perror("write post");
        return 1;
    }
    char post_out[16];
    memset(post_out, 0, sizeof(post_out));
    ssize_t n_post = read(m, post_out, sizeof(post_out) - 1);

    /* When OPOST is cleared */
    post_tio.c_oflag &= ~OPOST;
    tcsetattr(s, TCSANOW, &post_tio);
    if (write(s, "line\n", 5) < 0) {
        perror("write nopost");
        return 1;
    }
    char nopost_out[16];
    memset(nopost_out, 0, sizeof(nopost_out));
    ssize_t n_nopost = read(m, nopost_out, sizeof(nopost_out) - 1);
    if (n_post != 6 || memcmp(post_out, "line\r\n", 6) != 0 ||
        n_nopost != 5 || memcmp(nopost_out, "line\n", 5) != 0) {
        fprintf(stderr, "step 5: unexpected output bytes (%zd, %zd)\n", n_post, n_nopost);
        return 1;
    }
    printf("[5] Output translation: OPOST|ONLCR yielded %zd bytes (\\r\\n translation); bare OPOST=0 yielded %zd bytes (raw \\n)\n",
           n_post, n_nopost);

    /* Step 6: Raw mode transition (cfmakeraw) */
    struct termios raw_tio = orig;
    cfmakeraw(&raw_tio);
    if (tcsetattr(s, TCSANOW, &raw_tio) < 0) {
        perror("tcsetattr raw");
        return 1;
    }

    struct termios verified_raw;
    tcgetattr(s, &verified_raw);
    int raw_icanon = (verified_raw.c_lflag & ICANON) ? 1 : 0;
    int raw_echo   = (verified_raw.c_lflag & ECHO) ? 1 : 0;
    int raw_isig   = (verified_raw.c_lflag & ISIG) ? 1 : 0;
    int raw_opost  = (verified_raw.c_oflag & OPOST) ? 1 : 0;
    int raw_vmin   = verified_raw.c_cc[VMIN];
    int raw_vtime  = verified_raw.c_cc[VTIME];

    /* Send control bytes: VINTR (0x03), DEL (0x7f), WERASE (0x17) */
    const char raw_ctrl[] = "\x03\x7f\x17";
    if (write(m, raw_ctrl, 3) < 0) {
        perror("write raw ctrl");
        return 1;
    }
    char raw_rx[16];
    memset(raw_rx, 0, sizeof(raw_rx));
    ssize_t n_raw = 0;
    while (n_raw < 3) {
        ssize_t r = read(s, raw_rx + n_raw, sizeof(raw_rx) - 1 - n_raw);
        if (r <= 0) break;
        n_raw += r;
    }
    printf("[6] Raw mode transition: ICANON=%d ECHO=%d ISIG=%d OPOST=%d VMIN=%d VTIME=%d; control bytes delivered literally (0x%02x 0x%02x 0x%02x)\n",
           raw_icanon, raw_echo, raw_isig, raw_opost, raw_vmin, raw_vtime,
           (unsigned char)raw_rx[0], (unsigned char)raw_rx[1], (unsigned char)raw_rx[2]);

    /* Step 7 & 8: Signal generation and window size propagation via child session leader */
    int pipe_sync[2];
    if (pipe(pipe_sync) < 0) {
        perror("pipe");
        return 1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }

    if (pid == 0) {
        close(pipe_sync[0]);
        close(m);

        if (setsid() < 0) {
            perror("setsid");
            _exit(1);
        }
        if (ioctl(s, TIOCSCTTY, 0) < 0) {
            perror("TIOCSCTTY");
            _exit(1);
        }
        if (tcsetpgrp(s, getpid()) < 0) {
            perror("tcsetpgrp");
            _exit(1);
        }

        /* Restore ISIG for signal test */
        struct termios child_tio;
        tcgetattr(s, &child_tio);
        child_tio.c_lflag |= ISIG;
        tcsetattr(s, TCSANOW, &child_tio);

        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = handle_sigint;
        sigaction(SIGINT, &sa, NULL);
        sa.sa_handler = handle_sigwinch;
        sigaction(SIGWINCH, &sa, NULL);

        /* Notify parent child is ready */
        char sync_byte = 1;
        if (write(pipe_sync[1], &sync_byte, 1) != 1) _exit(1);

        /* Wait for SIGINT */
        while (!g_child_sigint) {
            usleep(1000);
        }

        /* Wait for SIGWINCH */
        while (!g_child_sigwinch) {
            usleep(1000);
        }

        struct winsize ws_child;
        if (ioctl(s, TIOCGWINSZ, &ws_child) < 0) {
            _exit(1);
        }

        if (ws_child.ws_row == 42 && ws_child.ws_col == 132) {
            sync_byte = 2;
            if (write(pipe_sync[1], &sync_byte, 1) != 1) _exit(1);
        }

        close(pipe_sync[1]);
        close(s);
        _exit(0);
    }

    close(pipe_sync[1]);
    char sync_ack = 0;
    if (read(pipe_sync[0], &sync_ack, 1) != 1 || sync_ack != 1) {
        fprintf(stderr, "Child process failed initialization\n");
        return 1;
    }

    /* Send VINTR (0x03) */
    char vintr = 0x03;
    if (write(m, &vintr, 1) < 0) {
        perror("write vintr");
        return 1;
    }

    /* Send TIOCSWINSZ window resize */
    struct winsize ws = { .ws_row = 42, .ws_col = 132, .ws_xpixel = 0, .ws_ypixel = 0 };
    if (ioctl(m, TIOCSWINSZ, &ws) < 0) {
        perror("ioctl TIOCSWINSZ");
        return 1;
    }

    if (read(pipe_sync[0], &sync_ack, 1) == 1 && sync_ack == 2) {
        printf("[7] Signal generation: master wrote VINTR (0x03), child foreground process group caught SIGINT\n");
        printf("[8] Window size propagation: TIOCSWINSZ set 42x132, child caught SIGWINCH and confirmed dimensions\n");
    } else {
        printf("[7] Signal/window propagation verification failed\n");
    }
    close(pipe_sync[0]);
    waitpid(pid, NULL, 0);

    /* Step 9: Non-canonical timing (VMIN and VTIME) */
    struct termios time_tio = orig;
    time_tio.c_lflag &= ~(ICANON | ECHO);
    time_tio.c_cc[VMIN] = 0;
    time_tio.c_cc[VTIME] = 2; /* 2 deciseconds = 200 ms timeout */
    tcsetattr(s, TCSANOW, &time_tio);

    struct timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);
    char timeout_buf[16];
    ssize_t n_timeout = read(s, timeout_buf, sizeof(timeout_buf));
    clock_gettime(CLOCK_MONOTONIC, &ts1);
    long elapsed_ms = (ts1.tv_sec - ts0.tv_sec) * 1000 + (ts1.tv_nsec - ts0.tv_nsec) / 1000000;
    int timed_out_ok = (n_timeout == 0 && elapsed_ms >= 150 && elapsed_ms <= 350) ? 1 : 0;

    /* VMIN=3, VTIME=0: blocks until 3 bytes are available */
    time_tio.c_cc[VMIN] = 3;
    time_tio.c_cc[VTIME] = 0;
    tcsetattr(s, TCSANOW, &time_tio);

    int pipe_block[2];
    if (pipe(pipe_block) < 0) {
        perror("pipe block");
        return 1;
    }
    pid = fork();
    if (pid == 0) {
        close(pipe_block[0]);
        char block_buf[4];
        ssize_t r = read(s, block_buf, 3);
        if (r == 3 && memcmp(block_buf, "xyz", 3) == 0) {
            char ok = 1;
            if (write(pipe_block[1], &ok, 1) != 1) _exit(1);
        }
        close(pipe_block[1]);
        close(s);
        _exit(0);
    }
    close(pipe_block[1]);
    /* Write 2 bytes: the child must stay blocked; poll its result pipe for 100 ms to prove it. */
    if (write(m, "xy", 2) != 2) {
        perror("write xy");
        return 1;
    }
    struct pollfd pfd = { .fd = pipe_block[0], .events = POLLIN, .revents = 0 };
    int early = poll(&pfd, 1, 100);
    int stayed_blocked = (early == 0) ? 1 : 0;
    /* Write 3rd byte: unblocks child */
    if (write(m, "z", 1) != 1) {
        perror("write z");
        return 1;
    }

    char block_ack = 0;
    if (read(pipe_block[0], &block_ack, 1) != 1) block_ack = 0;
    close(pipe_block[0]);
    waitpid(pid, NULL, 0);

    printf("[9] Non-canonical timing: VMIN=0/VTIME=2 timed out correctly (%s); VMIN=3/VTIME=0 blocked until 3rd byte arrived (%s)\n",
           timed_out_ok ? "PASS" : "FAIL", (block_ack == 1 && stayed_blocked) ? "PASS" : "FAIL");

    close(s);
    close(m);
    return 0;
}
