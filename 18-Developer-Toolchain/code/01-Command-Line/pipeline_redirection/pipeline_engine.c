#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <sys/wait.h>

static void test_dup2_cloexec(void) {
    int p[2];
    if (pipe(p) < 0) {
        perror("pipe");
        exit(1);
    }
    int flags = fcntl(p[0], F_GETFD);
    if (flags < 0) {
        perror("fcntl F_GETFD");
        exit(1);
    }
    if (fcntl(p[0], F_SETFD, flags | FD_CLOEXEC) < 0) {
        perror("fcntl F_SETFD");
        exit(1);
    }

    int old_cloexec = (fcntl(p[0], F_GETFD) & FD_CLOEXEC) ? 1 : 0;
    int target_fd = 10;
    if (dup2(p[0], target_fd) < 0) {
        perror("dup2");
        exit(1);
    }
    int new_cloexec = (fcntl(target_fd, F_GETFD) & FD_CLOEXEC) ? 1 : 0;

    printf("kernel_dup2_cloexec: old_fd_cloexec=%d target_fd_cloexec=%d\n", old_cloexec, new_cloexec);
    close(p[0]);
    close(p[1]);
    close(target_fd);
}

static void test_offset_sharing(const char *tmp_dir) {
    char path_shared[1024];
    char path_indep[1024];
    snprintf(path_shared, sizeof(path_shared), "%s/shared.txt", tmp_dir);
    snprintf(path_indep, sizeof(path_indep), "%s/indep.txt", tmp_dir);

    /* Test 1: dup() shares open file description and offset */
    int fd1 = open(path_shared, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd1 < 0) {
        perror("open shared");
        exit(1);
    }
    int fd2 = dup(fd1);
    if (fd2 < 0) {
        perror("dup");
        exit(1);
    }
    if (write(fd1, "AAAAA", 5) != 5 || write(fd2, "BBBBB", 5) != 5) {
        perror("write shared");
        exit(1);
    }
    close(fd1);
    close(fd2);

    char buf_shared[32] = {0};
    int rfd = open(path_shared, O_RDONLY);
    read(rfd, buf_shared, sizeof(buf_shared) - 1);
    close(rfd);
    unlink(path_shared);

    /* Test 2: independent open() calls create distinct file descriptions */
    int fda = open(path_indep, O_RDWR | O_CREAT | O_TRUNC, 0644);
    int fdb = open(path_indep, O_RDWR, 0644);
    if (fda < 0 || fdb < 0) {
        perror("open indep");
        exit(1);
    }
    if (write(fda, "AAAAA", 5) != 5 || write(fdb, "BBBBB", 5) != 5) {
        perror("write indep");
        exit(1);
    }
    close(fda);
    close(fdb);

    char buf_indep[32] = {0};
    rfd = open(path_indep, O_RDONLY);
    read(rfd, buf_indep, sizeof(buf_indep) - 1);
    close(rfd);
    unlink(path_indep);

    printf("kernel_offset_sharing: dup_shared=\"%s\" independent_open=\"%s\"\n", buf_shared, buf_indep);
}

static void test_unclosed_write_end(void) {
    int p[2];
    if (pipe(p) < 0) {
        perror("pipe");
        exit(1);
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid == 0) {
        /* Child: writer */
        close(p[0]);
        if (write(p[1], "PING", 4) != 4) {
            perror("child write");
        }
        close(p[1]);
        _exit(0);
    }

    /* Parent: reads from p[0] */
    waitpid(pid, NULL, 0);

    char buf[16] = {0};
    ssize_t n = read(p[0], buf, 4);
    (void)n;

    /* Case A: p[1] is still open in parent! Test if EOF is delivered */
    struct pollfd pfd;
    pfd.fd = p[0];
    pfd.events = POLLIN;
    int ret_leaked = poll(&pfd, 1, 30); /* 30 ms timeout */
    const char *status_leaked = (ret_leaked == 0) ? "TIMEOUT" : "DATA_OR_HUP";

    /* Case B: Close parent write end */
    close(p[1]);
    int ret_closed = poll(&pfd, 1, 30);
    char final_buf[16] = {0};
    /* Read only when poll() reported readiness, so a leaked writer cannot make this read block forever. */
    ssize_t final_read = (ret_closed > 0) ? read(p[0], final_buf, sizeof(final_buf)) : -1;
    const char *status_closed = (ret_closed > 0 && final_read == 0) ? "EOF" : "ERROR";

    printf("kernel_pipe_eof: with_leaked_writer=%s closed_writer=%s\n", status_leaked, status_closed);
    close(p[0]);
}

static void test_seek_check(const char *label) {
    off_t offset = lseek(STDIN_FILENO, 0, SEEK_CUR);
    if (offset == (off_t)-1) {
        if (errno == ESPIPE) {
            printf("%s: seekable=0 errno=ESPIPE\n", label);
        } else {
            printf("%s: seekable=0 errno=%d\n", label, errno);
        }
    } else {
        printf("%s: seekable=1 offset=%lld\n", label, (long long)offset);
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <dup2_cloexec|offset_sharing <dir>|unclosed_write_end|seek_check <label>>\n", argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "dup2_cloexec") == 0) {
        test_dup2_cloexec();
    } else if (strcmp(argv[1], "offset_sharing") == 0) {
        if (argc < 3) {
            fprintf(stderr, "offset_sharing requires tmp directory path\n");
            return 1;
        }
        test_offset_sharing(argv[2]);
    } else if (strcmp(argv[1], "unclosed_write_end") == 0) {
        test_unclosed_write_end();
    } else if (strcmp(argv[1], "seek_check") == 0) {
        const char *label = (argc >= 3) ? argv[2] : "seek_check";
        test_seek_check(label);
    } else {
        fprintf(stderr, "Unknown command: %s\n", argv[1]);
        return 1;
    }
    return 0;
}
