#define _GNU_SOURCE
#include <arpa/inet.h>
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/inet_diag.h>
#include <linux/netlink.h>
#include <sys/ptrace.h>
#include <linux/ptrace.h>
#include <linux/rtnetlink.h>
#include <linux/sock_diag.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

/*
 * Every value this lab prints is measured from the kernel or derived from a measured value.
 * The only printed values that are not measurements are the injected errno and the listen backlog,
 * which are the inputs of the experiment and are compared against what the kernel reports back.
 * Requires Linux >= 5.3 headers (PTRACE_GET_SYSCALL_INFO) on aarch64 or x86_64.
 */

#ifndef NT_ARM_SYSTEM_CALL
#define NT_ARM_SYSTEM_CALL 0x404
#endif

#if defined(__aarch64__)
#include <asm/ptrace.h>
#define LAB_AUDIT_ARCH AUDIT_ARCH_AARCH64

/* The kernel keeps the syscall number in regs->syscallno, not in x8: only this regset changes it. */
static int skip_syscall(pid_t pid) {
    int nr = -1;
    struct iovec iov = {.iov_base = &nr, .iov_len = sizeof(nr)};
    return (int)ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_ARM_SYSTEM_CALL, &iov);
}

static int set_return_value(pid_t pid, long value) {
    struct user_pt_regs regs;
    struct iovec iov = {.iov_base = &regs, .iov_len = sizeof(regs)};
    if (ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) != 0) return -1;
    regs.regs[0] = (unsigned long)value;
    return (int)ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
}
#elif defined(__x86_64__)
#include <sys/user.h>
#define LAB_AUDIT_ARCH AUDIT_ARCH_X86_64

static int skip_syscall(pid_t pid) {
    struct user_regs_struct regs;
    struct iovec iov = {.iov_base = &regs, .iov_len = sizeof(regs)};
    if (ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) != 0) return -1;
    regs.orig_rax = (unsigned long)-1;
    return (int)ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
}

static int set_return_value(pid_t pid, long value) {
    struct user_regs_struct regs;
    struct iovec iov = {.iov_base = &regs, .iov_len = sizeof(regs)};
    if (ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) != 0) return -1;
    regs.rax = (unsigned long)value;
    return (int)ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
}
#else
#error "Unsupported architecture for register tampering in lab"
#endif

#define CREATE_FLAGS (O_WRONLY | O_CREAT | O_EXCL)

struct child_report {
    int skipped_ret;
    int skipped_errno;
    int control_ret;
    int control_errno;
};

static int path_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

/* 1, 2, 3, 4: ptrace syscall tracing, the syscall info API, fault injection, and /proc status. */
static void test_ptrace_and_proc_status(const char *dir) {
    char skipped_path[256], control_path[256];
    snprintf(skipped_path, sizeof(skipped_path), "%s/skipped.txt", dir);
    snprintf(control_path, sizeof(control_path), "%s/control.txt", dir);

    int pipefd[2];
    if (pipe(pipefd) < 0) {
        perror("pipe");
        exit(1);
    }

    const int injected_errno = ENOENT;
    pid_t parent_pid = getpid();
    pid_t child = fork();
    if (child == 0) {
        close(pipefd[0]);
        if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) < 0) {
            perror("PTRACE_TRACEME");
            _exit(1);
        }
        raise(SIGSTOP);

        (void)syscall(SYS_getpid);

        /* The tracer skips this call and injects an error. A skipped O_CREAT|O_EXCL leaves no file behind. */
        struct child_report rep;
        int fd = openat(AT_FDCWD, skipped_path, CREATE_FLAGS, 0600);
        rep.skipped_ret = fd;
        rep.skipped_errno = fd < 0 ? errno : 0;
        if (fd >= 0) close(fd);

        /* Control: the second openat is not tampered with, so it must create its file. */
        fd = openat(AT_FDCWD, control_path, CREATE_FLAGS, 0600);
        rep.control_ret = fd;
        rep.control_errno = fd < 0 ? errno : 0;
        if (fd >= 0) close(fd);

        if (write(pipefd[1], &rep, sizeof(rep)) != (ssize_t)sizeof(rep)) _exit(1);
        close(pipefd[1]);
        _exit(0);
    }

    close(pipefd[1]);

    int status;
    waitpid(child, &status, 0); /* the initial SIGSTOP (signal-delivery-stop) */
    int initial_stopsig = WIFSTOPPED(status) ? WSTOPSIG(status) : 0;

    char status_path[64];
    snprintf(status_path, sizeof(status_path), "/proc/%d/status", child);
    FILE *fstatus = fopen(status_path, "r");
    char state_char = '?';
    int parsed_tracer_pid = -1;
    if (fstatus) {
        char line[256];
        while (fgets(line, sizeof(line), fstatus)) {
            if (strncmp(line, "State:", 6) == 0) {
                sscanf(line + 6, " %c", &state_char);
            } else if (strncmp(line, "TracerPid:", 10) == 0) {
                sscanf(line + 10, "%d", &parsed_tracer_pid);
            }
        }
        fclose(fstatus);
    }

    ptrace(PTRACE_SETOPTIONS, child, 0, PTRACE_O_TRACESYSGOOD);

    int first_stop_sig = 0, first_stop_status = 0;
    int stops = 0, first_op = 0, second_op = 0;
    int expect_entry = 1, alternates = 1, arch_ok = 1, info_ok = 1;
    int getpid_seen = 0, getpid_rval_is_child_pid = 0;
    int openat_entries = 0, skip_pending = 0, tamper_ok = 1;
    long last_nr = -1;
    int resume_sig = 0, child_exit = -1;

    for (;;) {
        if (ptrace(PTRACE_SYSCALL, child, 0, resume_sig) < 0) break;
        resume_sig = 0;
        if (waitpid(child, &status, 0) < 0) break;
        if (WIFEXITED(status)) {
            child_exit = WEXITSTATUS(status);
            break;
        }
        if (WIFSIGNALED(status)) {
            child_exit = 128 + WTERMSIG(status);
            break;
        }
        if (!WIFSTOPPED(status)) continue;

        int sig = WSTOPSIG(status);
        if (first_stop_sig == 0) {
            first_stop_sig = sig;
            first_stop_status = status;
        }
        if (sig != (SIGTRAP | 0x80)) {
            if (sig != SIGTRAP) resume_sig = sig;
            continue;
        }

        struct ptrace_syscall_info info;
        memset(&info, 0, sizeof(info));
        if (ptrace(PTRACE_GET_SYSCALL_INFO, child, (void *)sizeof(info), &info) <= 0) {
            info_ok = 0;
            continue;
        }
        stops++;
        if (stops == 1) first_op = info.op;
        if (stops == 2) second_op = info.op;
        if (info.arch != LAB_AUDIT_ARCH) arch_ok = 0;
        if (info.op != PTRACE_SYSCALL_INFO_ENTRY && info.op != PTRACE_SYSCALL_INFO_EXIT) alternates = 0;
        if ((info.op == PTRACE_SYSCALL_INFO_ENTRY) != expect_entry) alternates = 0;
        expect_entry = (info.op != PTRACE_SYSCALL_INFO_ENTRY);

        if (info.op == PTRACE_SYSCALL_INFO_ENTRY) {
            last_nr = (long)info.entry.nr;
            if (info.entry.nr == (uint64_t)SYS_getpid) {
                getpid_seen = 1;
            } else if (info.entry.nr == (uint64_t)SYS_openat && openat_entries++ == 0) {
                skip_pending = 1;
                if (skip_syscall(child) != 0) tamper_ok = 0;
            }
        } else if (info.op == PTRACE_SYSCALL_INFO_EXIT) {
            if (last_nr == (long)SYS_getpid && info.exit.rval == (int64_t)child) getpid_rval_is_child_pid = 1;
            if (skip_pending) {
                skip_pending = 0;
                if (set_return_value(child, -(long)injected_errno) != 0) tamper_ok = 0;
            }
        }
    }

    struct child_report rep;
    memset(&rep, 0, sizeof(rep));
    rep.skipped_ret = rep.control_ret = -99;
    if (read(pipefd[0], &rep, sizeof(rep)) != (ssize_t)sizeof(rep)) rep.skipped_ret = rep.control_ret = -99;
    close(pipefd[0]);

    int skipped_file_absent = !path_exists(skipped_path);
    int control_file_present = path_exists(control_path);
    unlink(skipped_path);
    unlink(control_path);

    printf("[1] ptrace_syscall_interception: initial_sigstop=%d first_stop_sig=%d raw_status=0x%x"
           " getpid_seen=%d getpid_rval_is_child_pid=%d\n",
           initial_stopsig, first_stop_sig, (unsigned)first_stop_status, getpid_seen,
           getpid_rval_is_child_pid);
    printf("[2] ptrace_syscall_info_api: first_op=%d second_op=%d alternates=%d arch_ok=%d info_ok=%d\n",
           first_op, second_op, alternates && stops > 0, arch_ok, info_ok);
    printf("[3] ptrace_fault_injection: injected_errno=%d child_ret=%d child_errno=%d"
           " skipped_file_absent=%d control_ret_ok=%d control_file_present=%d tamper_ok=%d child_exit=%d\n",
           injected_errno, rep.skipped_ret, rep.skipped_errno, skipped_file_absent, rep.control_ret >= 0,
           control_file_present, tamper_ok, child_exit);
    printf("[4] proc_process_state: state_during_ptrace_stop=%c tracer_pid_matches=%d\n", state_char,
           parsed_tracer_pid == parent_pid);
}

/* Reads one numeric field such as "pos:\t16384" from a /proc/self/fdinfo file; -1 if absent. */
static long fdinfo_field(int fd, const char *key, int base) {
    char info_path[64];
    snprintf(info_path, sizeof(info_path), "/proc/self/fdinfo/%d", fd);
    FILE *f = fopen(info_path, "r");
    if (!f) return -1;
    char line[256];
    long value = -1;
    size_t klen = strlen(key);
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, key, klen) == 0) {
            value = strtol(line + klen, NULL, base);
            break;
        }
    }
    fclose(f);
    return value;
}

/* 5: file descriptor inspection via /proc/self/fd and fdinfo, including an unlinked-but-open file. */
static void test_proc_fd_inspection(const char *dir) {
    int null_fd = open("/dev/null", O_RDONLY);
    if (null_fd < 0) {
        perror("open /dev/null");
        exit(1);
    }
    char link_path[64], target[256] = {0};
    snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", null_fd);
    ssize_t r = readlink(link_path, target, sizeof(target) - 1);
    int devnull_resolves = (r > 0 && strcmp(target, "/dev/null") == 0);
    close(null_fd);

    char path[256];
    snprintf(path, sizeof(path), "%s/leaked.log", dir);
    int fd = open(path, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        perror("open leaked.log");
        exit(1);
    }
    char block[4096];
    memset(block, 'x', sizeof(block));
    for (int i = 0; i < 4; i++) {
        if (write(fd, block, sizeof(block)) != (ssize_t)sizeof(block)) exit(1);
    }
    unlink(path); /* the name is gone; the inode lives on while the descriptor is open */

    snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", fd);
    memset(target, 0, sizeof(target));
    r = readlink(link_path, target, sizeof(target) - 1);
    static const char suffix[] = " (deleted)";
    int deleted_suffix = (r > (ssize_t)sizeof(suffix) - 1 && strcmp(target + r - (sizeof(suffix) - 1), suffix) == 0);

    long pos = fdinfo_field(fd, "pos:", 10);
    long flags = fdinfo_field(fd, "flags:", 8);
    int flags_rdwr = (flags >= 0 && (flags & O_ACCMODE) == O_RDWR);

    struct stat st;
    long nlink = fstat(fd, &st) == 0 ? (long)st.st_nlink : -1;
    long size_before = (long)st.st_size;

    /* "truncate -s 0 /proc/<pid>/fd/<fd>" opens the magic link, which reaches the unlinked inode. */
    int truncated = (truncate(link_path, 0) == 0);
    long size_after_truncate = (fstat(fd, &st) == 0) ? (long)st.st_size : -1;
    long blocks_after_truncate = (long)st.st_blocks;

    /* The descriptor keeps its offset (not O_APPEND), so the next write leaves a hole before it. */
    if (write(fd, "y", 1) != 1) exit(1);
    long size_after_write = (fstat(fd, &st) == 0) ? (long)st.st_size : -1;
    int sparse = ((long)st.st_blocks * 512 < size_after_write);
    close(fd);

    printf("[5] proc_fd_and_fdinfo: devnull_resolves=%d pos=%ld flags_rdwr=%d deleted_suffix=%d nlink=%ld"
           " size_before=%ld truncated=%d size_after_truncate=%ld blocks_after_truncate=%ld"
           " size_after_write=%ld sparse=%d\n",
           devnull_resolves, pos, flags_rdwr, deleted_suffix, nlink, size_before, truncated,
           size_after_truncate, blocks_after_truncate, size_after_write, sparse);
}

struct diag_match {
    int sport;
    int dport; /* 0 selects a listening socket */
    int found;
    unsigned state, rqueue, wqueue;
};

static void diag_match_one(const struct inet_diag_msg *m, struct diag_match *f) {
    if (f->found) return;
    if (ntohs(m->id.idiag_sport) == f->sport && ntohs(m->id.idiag_dport) == f->dport) {
        f->found = 1;
        f->state = m->idiag_state;
        f->rqueue = m->idiag_rqueue;
        f->wqueue = m->idiag_wqueue;
    }
}

/* Dumps TCP sockets in the given state mask over NETLINK_SOCK_DIAG, as `ss` does, until NLMSG_DONE. */
static void diag_query(unsigned states, struct diag_match *f) {
    int nl_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_SOCK_DIAG);
    if (nl_fd < 0) {
        perror("netlink socket");
        exit(1);
    }
    struct {
        struct nlmsghdr nlh;
        struct inet_diag_req_v2 req;
    } msg;
    memset(&msg, 0, sizeof(msg));
    msg.nlh.nlmsg_len = sizeof(msg);
    msg.nlh.nlmsg_type = SOCK_DIAG_BY_FAMILY;
    msg.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP;
    msg.req.sdiag_family = AF_INET;
    msg.req.sdiag_protocol = IPPROTO_TCP;
    msg.req.idiag_states = states;
    if (send(nl_fd, &msg, sizeof(msg), 0) < 0) {
        perror("send netlink");
        exit(1);
    }

    char buf[32768];
    int done = 0;
    while (!done) {
        ssize_t n = recv(nl_fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        int len = (int)n;
        for (struct nlmsghdr *nlh = (struct nlmsghdr *)buf; NLMSG_OK(nlh, len); nlh = NLMSG_NEXT(nlh, len)) {
            if (nlh->nlmsg_type == NLMSG_DONE || nlh->nlmsg_type == NLMSG_ERROR) {
                done = 1;
                break;
            }
            diag_match_one((struct inet_diag_msg *)NLMSG_DATA(nlh), f);
        }
    }
    close(nl_fd);
}

static int local_port(int fd) {
    struct sockaddr_in sin;
    socklen_t slen = sizeof(sin);
    if (getsockname(fd, (struct sockaddr *)&sin, &slen) != 0) return -1;
    return ntohs(sin.sin_port);
}

/* 6: socket queues via Netlink sock_diag: accept queue on a listener, receive queue on an established socket. */
static void test_socket_queue_inspection(void) {
    const int backlog = 5;
    int sfd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in sin;
    memset(&sin, 0, sizeof(sin));
    sin.sin_family = AF_INET;
    sin.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sin.sin_port = 0;
    if (bind(sfd, (struct sockaddr *)&sin, sizeof(sin)) != 0 || listen(sfd, backlog) != 0) {
        perror("bind/listen");
        exit(1);
    }
    int port = local_port(sfd);
    sin.sin_port = htons((uint16_t)port);

    int c1 = socket(AF_INET, SOCK_STREAM, 0);
    int c2 = socket(AF_INET, SOCK_STREAM, 0);
    if (connect(c1, (struct sockaddr *)&sin, sizeof(sin)) != 0 ||
        connect(c2, (struct sockaddr *)&sin, sizeof(sin)) != 0) {
        perror("connect");
        exit(1);
    }

    struct diag_match listener = {.sport = port, .dport = 0};
    diag_query(1u << TCP_LISTEN, &listener);

    int cfd = accept(sfd, NULL, NULL);
    struct diag_match after_accept = {.sport = port, .dport = 0};
    diag_query(1u << TCP_LISTEN, &after_accept);

    /* Data written by a client and not yet read sits in the server-side socket's Recv-Q. */
    char payload[100];
    memset(payload, 'z', sizeof(payload));
    if (write(c1, payload, sizeof(payload)) != (ssize_t)sizeof(payload)) exit(1);
    struct diag_match estab = {.sport = port, .dport = local_port(c1)};
    for (int i = 0; i < 200; i++) {
        estab.found = 0;
        diag_query(1u << TCP_ESTABLISHED, &estab);
        if (estab.found && estab.rqueue == sizeof(payload)) break;
        usleep(10000);
    }

    if (cfd >= 0) close(cfd);
    close(c1);
    close(c2);
    close(sfd);

    printf("[6] socket_queue_sock_diag: found_listener=%d tcp_listen_state=%u recv_q_pending=%u"
           " send_q_backlog=%u backlog_arg=%d recv_q_after_accept=%u"
           " found_estab=%d estab_state=%u estab_recv_q=%u estab_send_q=%u\n",
           listener.found, listener.state, listener.rqueue, listener.wqueue, backlog, after_accept.rqueue,
           estab.found, estab.state, estab.rqueue, estab.wqueue);
}

int main(void) {
    char dir[] = "/tmp/strace_sys_inspect.XXXXXX";
    if (!mkdtemp(dir)) {
        perror("mkdtemp");
        return 1;
    }
    test_ptrace_and_proc_status(dir);
    test_proc_fd_inspection(dir);
    test_socket_queue_inspection();
    rmdir(dir);
    return 0;
}
