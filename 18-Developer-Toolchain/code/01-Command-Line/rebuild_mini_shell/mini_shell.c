#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define MAX_LINE 4096
#define MAX_ARGS 256
#define MAX_PROCESSES 64

typedef enum {
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_COMPLETED
} ProcessState;

typedef struct Process {
    struct Process *next;
    char **argv;
    pid_t pid;
    ProcessState state;
    int exit_status;
} Process;

typedef struct Job {
    struct Job *next;
    int id;
    pid_t pgid;
    char *command_line;
    Process *first_process;
    struct termios tmodes;
    bool foreground;
    bool notified;
    char *infile;
    char *outfile;
    bool append_out;
    char *errfile;
    bool merge_err;
} Job;

static Job *job_list_head = NULL;
static int next_job_id = 1;
static pid_t shell_pgid = 0;
static struct termios shell_tmodes;
static int shell_terminal = STDIN_FILENO;
static bool shell_is_interactive = false;

/* Test hook: MINI_SHELL_RACE_DELAY=parent|child sleeps 150 ms before that side's setpgid(),
 * so the lab can prove that each half of the dual setpgid() is sufficient on its own. */
typedef enum { RACE_NONE, RACE_PARENT, RACE_CHILD } RaceDelay;
static RaceDelay race_delay = RACE_NONE;

static void diag(const char *what) {
    int saved = errno;
    fprintf(stderr, "mini_sh: %s: %s\n", what, strerror(saved));
}

static void race_pause(RaceDelay side) {
    if (race_delay != side) return;
    struct timespec ts = {0, 150000000L};
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {
    }
}

static void free_process_list(Process *p) {
    while (p) {
        Process *next = p->next;
        if (p->argv) {
            for (int i = 0; p->argv[i]; ++i) {
                free(p->argv[i]);
            }
            free(p->argv);
        }
        free(p);
        p = next;
    }
}

static void free_job(Job *j) {
    if (!j) return;
    free_process_list(j->first_process);
    free(j->command_line);
    free(j->infile);
    free(j->outfile);
    free(j->errfile);
    free(j);
}

static void add_job(Job *j) {
    j->id = next_job_id++;
    j->next = NULL;
    if (!job_list_head) {
        job_list_head = j;
    } else {
        Job *curr = job_list_head;
        while (curr->next) {
            curr = curr->next;
        }
        curr->next = j;
    }
}

static void remove_job(Job *j) {
    if (!job_list_head || !j) return;
    if (job_list_head == j) {
        job_list_head = j->next;
        free_job(j);
        return;
    }
    Job *curr = job_list_head;
    while (curr->next && curr->next != j) {
        curr = curr->next;
    }
    if (curr->next == j) {
        curr->next = j->next;
        free_job(j);
    }
}

static Job *find_job_by_id(int id) {
    for (Job *j = job_list_head; j; j = j->next) {
        if (j->id == id) return j;
    }
    return NULL;
}


static bool job_is_stopped(const Job *j) {
    for (Process *p = j->first_process; p; p = p->next) {
        if (p->state == STATUS_RUNNING) return false;
    }
    for (Process *p = j->first_process; p; p = p->next) {
        if (p->state == STATUS_STOPPED) return true;
    }
    return false;
}

static bool job_is_completed(const Job *j) {
    for (Process *p = j->first_process; p; p = p->next) {
        if (p->state != STATUS_COMPLETED) return false;
    }
    return true;
}

static bool mark_process_status(pid_t pid, int status) {
    if (pid <= 0) return false;
    for (Job *j = job_list_head; j; j = j->next) {
        for (Process *p = j->first_process; p; p = p->next) {
            if (p->pid == pid) {
                p->exit_status = status;
                if (WIFSTOPPED(status)) {
                    p->state = STATUS_STOPPED;
                } else if (WIFCONTINUED(status)) {
                    p->state = STATUS_RUNNING;
                    j->notified = false;
                } else {
                    p->state = STATUS_COMPLETED;
                }
                return true;
            }
        }
    }
    return false;
}

static void check_jobs(void) {
    int status;
    pid_t pid;

    while ((pid = waitpid(-1, &status, WNOHANG | WUNTRACED | WCONTINUED)) > 0) {
        mark_process_status(pid, status);
    }

    Job *j = job_list_head;
    while (j) {
        Job *next = j->next;
        if (job_is_completed(j)) {
            if (!j->foreground) {
                printf("[%d]+ Done %s\n", j->id, j->command_line);
                fflush(stdout);
            }
            remove_job(j);
        } else if (job_is_stopped(j) && !j->notified) {
            printf("[%d]+ Stopped %s\n", j->id, j->command_line);
            fflush(stdout);
            j->notified = true;
        }
        j = next;
    }
}

static void init_shell(void) {
    shell_is_interactive = isatty(shell_terminal);

    if (shell_is_interactive) {
        while (tcgetpgrp(shell_terminal) != (shell_pgid = getpgrp())) {
            kill(-shell_pgid, SIGTTIN);
        }

        signal(SIGINT, SIG_IGN);
        signal(SIGQUIT, SIG_IGN);
        signal(SIGTSTP, SIG_IGN);
        signal(SIGTTIN, SIG_IGN);
        signal(SIGTTOU, SIG_IGN);

        shell_pgid = getpid();
        if (setpgid(shell_pgid, shell_pgid) < 0) {
            if (errno != EPERM || getsid(0) != shell_pgid) {
                perror("Couldn't put the shell in its own process group");
                exit(1);
            }
        }

        if (tcsetpgrp(shell_terminal, shell_pgid) < 0) diag("tcsetpgrp (init)");
        tcgetattr(shell_terminal, &shell_tmodes);
    }
}

static void put_job_in_foreground(Job *j, bool continue_job) {
    j->foreground = true;
    if (shell_is_interactive) {
        if (tcsetpgrp(shell_terminal, j->pgid) < 0) diag("tcsetpgrp (handoff)");
    }

    if (continue_job) {
        if (shell_is_interactive) {
            tcsetattr(shell_terminal, TCSADRAIN, &j->tmodes);
        }
        if (kill(-j->pgid, SIGCONT) < 0) {
            perror("kill (SIGCONT)");
        }
        for (Process *p = j->first_process; p; p = p->next) {
            if (p->state == STATUS_STOPPED) {
                p->state = STATUS_RUNNING;
            }
        }
        j->notified = false;
    }

    int status;
    pid_t pid;
    while (!job_is_stopped(j) && !job_is_completed(j)) {
        pid = waitpid(-j->pgid, &status, WUNTRACED);
        if (pid <= 0) break;
        mark_process_status(pid, status);
    }

    if (shell_is_interactive) {
        if (tcsetpgrp(shell_terminal, shell_pgid) < 0) diag("tcsetpgrp (reclaim)");
        tcgetattr(shell_terminal, &j->tmodes);
        tcsetattr(shell_terminal, TCSADRAIN, &shell_tmodes);
    }

    if (job_is_stopped(j)) {
        printf("\n[%d]+ Stopped %s\n", j->id, j->command_line);
        fflush(stdout);
        j->notified = true;
    } else if (job_is_completed(j)) {
        remove_job(j);
    }
}

static void put_job_in_background(Job *j, bool continue_job) {
    j->foreground = false;
    if (continue_job) {
        if (kill(-j->pgid, SIGCONT) < 0) {
            perror("kill (SIGCONT)");
        }
        for (Process *p = j->first_process; p; p = p->next) {
            if (p->state == STATUS_STOPPED) {
                p->state = STATUS_RUNNING;
            }
        }
        j->notified = false;
    }
    printf("[%d] %d\n", j->id, (int)j->pgid);
    fflush(stdout);
}

static int builtin_cd(char **argv) {
    const char *target = argv[1];
    if (!target) {
        target = getenv("HOME");
        if (!target) target = "/";
    }
    if (chdir(target) != 0) {
        perror("cd");
        return 1;
    }
    return 0;
}

static int builtin_jobs(void) {
    check_jobs();
    for (Job *j = job_list_head; j; j = j->next) {
        const char *state_str = "Running";
        if (job_is_stopped(j)) {
            state_str = "Stopped";
        }
        printf("[%d]+ %-10s %s\n", j->id, state_str, j->command_line);
    }
    fflush(stdout);
    return 0;
}

static int builtin_fg(char **argv) {
    int id = 0;
    if (argv[1]) {
        const char *s = argv[1];
        if (s[0] == '%') s++;
        id = atoi(s);
    } else {
        Job *last = NULL;
        for (Job *j = job_list_head; j; j = j->next) last = j;
        if (last) id = last->id;
    }

    Job *j = find_job_by_id(id);
    if (!j) {
        fprintf(stderr, "fg: %s: no such job\n", argv[1] ? argv[1] : "current");
        return 1;
    }
    printf("%s\n", j->command_line);
    fflush(stdout);
    put_job_in_foreground(j, true);
    return 0;
}

static int builtin_bg(char **argv) {
    int id = 0;
    if (argv[1]) {
        const char *s = argv[1];
        if (s[0] == '%') s++;
        id = atoi(s);
    } else {
        Job *last = NULL;
        for (Job *j = job_list_head; j; j = j->next) last = j;
        if (last) id = last->id;
    }

    Job *j = find_job_by_id(id);
    if (!j) {
        fprintf(stderr, "bg: %s: no such job\n", argv[1] ? argv[1] : "current");
        return 1;
    }
    put_job_in_background(j, true);
    return 0;
}

static int builtin_pgid(void) {
    printf("shell_pgid: %d, terminal_fg: %d\n",
           (int)getpgrp(),
           shell_is_interactive ? (int)tcgetpgrp(shell_terminal) : -1);
    fflush(stdout);
    return 0;
}

static bool is_builtin(const char *cmd) {
    if (!cmd) return false;
    return (strcmp(cmd, "cd") == 0 ||
            strcmp(cmd, "jobs") == 0 ||
            strcmp(cmd, "fg") == 0 ||
            strcmp(cmd, "bg") == 0 ||
            strcmp(cmd, "pgid") == 0 ||
            strcmp(cmd, "exit") == 0);
}

static int execute_builtin(char **argv) {
    if (strcmp(argv[0], "cd") == 0) return builtin_cd(argv);
    if (strcmp(argv[0], "jobs") == 0) return builtin_jobs();
    if (strcmp(argv[0], "fg") == 0) return builtin_fg(argv);
    if (strcmp(argv[0], "bg") == 0) return builtin_bg(argv);
    if (strcmp(argv[0], "pgid") == 0) return builtin_pgid();
    if (strcmp(argv[0], "exit") == 0) {
        int code = argv[1] ? atoi(argv[1]) : 0;
        exit(code);
    }
    return 1;
}

static char *trim_whitespace(char *str) {
    while (isspace((unsigned char)*str)) str++;
    if (*str == 0) return str;
    char *end = str + strlen(str) - 1;
    while (end > str && isspace((unsigned char)*end)) end--;
    end[1] = '\0';
    return str;
}

static Process *parse_process(char *segment, Job *job) {
    Process *proc = calloc(1, sizeof(Process));
    if (!proc) return NULL;

    char **argv = malloc(sizeof(char *) * MAX_ARGS);
    int argc = 0;

    char *p = segment;
    while (*p && argc < MAX_ARGS - 1) {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;

        if (*p == '<') {
            p++;
            while (*p && isspace((unsigned char)*p)) p++;
            char *start = p;
            while (*p && !isspace((unsigned char)*p) && *p != '<' && *p != '>' && *p != '|') p++;
            char tmp = *p;
            *p = '\0';
            free(job->infile);
            job->infile = strdup(start);
            *p = tmp;
            continue;
        }

        if (p[0] == '2' && p[1] == '>' && p[2] == '&' && p[3] == '1') {
            job->merge_err = true;
            p += 4;
            continue;
        }

        if (p[0] == '2' && p[1] == '>') {
            p += 2;
            while (*p && isspace((unsigned char)*p)) p++;
            char *start = p;
            while (*p && !isspace((unsigned char)*p) && *p != '<' && *p != '>' && *p != '|') p++;
            char tmp = *p;
            *p = '\0';
            free(job->errfile);
            job->errfile = strdup(start);
            *p = tmp;
            continue;
        }

        if (p[0] == '>' && p[1] == '>') {
            job->append_out = true;
            p += 2;
            while (*p && isspace((unsigned char)*p)) p++;
            char *start = p;
            while (*p && !isspace((unsigned char)*p) && *p != '<' && *p != '>' && *p != '|') p++;
            char tmp = *p;
            *p = '\0';
            free(job->outfile);
            job->outfile = strdup(start);
            *p = tmp;
            continue;
        }

        if (*p == '>') {
            job->append_out = false;
            p++;
            while (*p && isspace((unsigned char)*p)) p++;
            char *start = p;
            while (*p && !isspace((unsigned char)*p) && *p != '<' && *p != '>' && *p != '|') p++;
            char tmp = *p;
            *p = '\0';
            free(job->outfile);
            job->outfile = strdup(start);
            *p = tmp;
            continue;
        }

        char token[MAX_LINE];
        int tlen = 0;
        while (*p && !isspace((unsigned char)*p) && *p != '<' && *p != '>' && *p != '|') {
            if (*p == '\'' || *p == '"') {
                char quote = *p++;
                while (*p && *p != quote) {
                    token[tlen++] = *p++;
                }
                if (*p == quote) p++;
            } else {
                token[tlen++] = *p++;
            }
        }
        token[tlen] = '\0';
        argv[argc++] = strdup(token);
    }
    argv[argc] = NULL;
    proc->argv = argv;
    return proc;
}

static Job *parse_command_line(char *line) {
    line = trim_whitespace(line);
    if (!*line || line[0] == '#') return NULL;

    Job *job = calloc(1, sizeof(Job));
    if (!job) return NULL;
    job->command_line = strdup(line);
    job->foreground = true;

    size_t len = strlen(line);
    if (len > 0 && line[len - 1] == '&') {
        job->foreground = false;
        line[len - 1] = '\0';
        line = trim_whitespace(line);
    }

    char *saveptr;
    char *pipe_seg = strtok_r(line, "|", &saveptr);
    Process *last_proc = NULL;

    while (pipe_seg) {
        Process *proc = parse_process(pipe_seg, job);
        if (proc && proc->argv && proc->argv[0]) {
            if (!job->first_process) {
                job->first_process = proc;
            } else {
                last_proc->next = proc;
            }
            last_proc = proc;
        } else if (proc) {
            free_process_list(proc);
        }
        pipe_seg = strtok_r(NULL, "|", &saveptr);
    }

    if (!job->first_process) {
        free_job(job);
        return NULL;
    }

    return job;
}

static void launch_job(Job *job) {
    if (!job->first_process->next && is_builtin(job->first_process->argv[0])) {
        execute_builtin(job->first_process->argv);
        free_job(job);
        return;
    }

    add_job(job);

    int pipefd[2] = {-1, -1};
    int in_fd = STDIN_FILENO;
    int out_fd = STDOUT_FILENO;

    if (job->infile) {
        in_fd = open(job->infile, O_RDONLY);
        if (in_fd < 0) {
            perror("open (infile)");
            remove_job(job);
            return;
        }
    }

    int job_out_fd = -1;
    if (job->outfile) {
        int flags = O_WRONLY | O_CREAT | (job->append_out ? O_APPEND : O_TRUNC);
        job_out_fd = open(job->outfile, flags, 0666);
        if (job_out_fd < 0) {
            perror("open (outfile)");
            if (in_fd != STDIN_FILENO) close(in_fd);
            remove_job(job);
            return;
        }
    }

    int job_err_fd = -1;
    if (job->errfile) {
        job_err_fd = open(job->errfile, O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (job_err_fd < 0) {
            perror("open (errfile)");
            if (in_fd != STDIN_FILENO) close(in_fd);
            if (job_out_fd >= 0) close(job_out_fd);
            remove_job(job);
            return;
        }
    }

    Process *p;
    for (p = job->first_process; p; p = p->next) {
        if (p->next) {
            if (pipe(pipefd) < 0) {
                perror("pipe");
                return;
            }
            out_fd = pipefd[1];
        } else {
            out_fd = (job_out_fd >= 0) ? job_out_fd : STDOUT_FILENO;
        }

        pid_t pid = fork();
        if (pid < 0) {
            perror("fork");
            return;
        }

        if (pid == 0) {
            pid_t child_pid = getpid();
            if (job->pgid == 0) job->pgid = child_pid;
            race_pause(RACE_CHILD);
            if (setpgid(child_pid, job->pgid) < 0) diag("setpgid (child)");

            if (job->foreground && shell_is_interactive) {
                if (tcsetpgrp(shell_terminal, job->pgid) < 0) diag("tcsetpgrp (child)");
            }

            signal(SIGINT, SIG_DFL);
            signal(SIGQUIT, SIG_DFL);
            signal(SIGTSTP, SIG_DFL);
            signal(SIGTTIN, SIG_DFL);
            signal(SIGTTOU, SIG_DFL);
            signal(SIGCHLD, SIG_DFL);

            if (in_fd != STDIN_FILENO) {
                dup2(in_fd, STDIN_FILENO);
                close(in_fd);
            }
            if (out_fd != STDOUT_FILENO) {
                dup2(out_fd, STDOUT_FILENO);
                close(out_fd);
            }
            if (pipefd[0] != -1) {
                close(pipefd[0]);
            }

            if (job_err_fd >= 0) {
                dup2(job_err_fd, STDERR_FILENO);
                close(job_err_fd);
            } else if (job->merge_err) {
                dup2(STDOUT_FILENO, STDERR_FILENO);
            }

            if (is_builtin(p->argv[0])) {
                int ret = execute_builtin(p->argv);
                _exit(ret);
            }

            execvp(p->argv[0], p->argv);
            fprintf(stderr, "mini_sh: %s: command not found\n", p->argv[0]);
            _exit(127);
        } else {
            p->pid = pid;
            if (job->pgid == 0) job->pgid = pid;
            race_pause(RACE_PARENT);
            /* EACCES means the child already exec'd after its own setpgid(); ESRCH means it already exited. */
            if (setpgid(pid, job->pgid) < 0 && errno != EACCES && errno != ESRCH) diag("setpgid (parent)");

            if (in_fd != STDIN_FILENO) {
                close(in_fd);
            }
            if (out_fd != STDOUT_FILENO && out_fd != job_out_fd) {
                close(out_fd);
            }
            in_fd = pipefd[0];
            pipefd[0] = -1;
            pipefd[1] = -1;
        }
    }

    if (job_out_fd >= 0) close(job_out_fd);
    if (job_err_fd >= 0) close(job_err_fd);

    if (!shell_is_interactive) {
        put_job_in_foreground(job, false);
    } else if (job->foreground) {
        put_job_in_foreground(job, false);
    } else {
        put_job_in_background(job, false);
    }
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    const char *delay = getenv("MINI_SHELL_RACE_DELAY");
    if (delay && strcmp(delay, "parent") == 0) race_delay = RACE_PARENT;
    if (delay && strcmp(delay, "child") == 0) race_delay = RACE_CHILD;

    init_shell();

    char line[MAX_LINE];
    while (1) {
        check_jobs();

        if (shell_is_interactive) {
            printf("mini_sh> ");
            fflush(stdout);
        }

        if (!fgets(line, sizeof(line), stdin)) {
            if (shell_is_interactive) {
                printf("\n");
            }
            break;
        }

        Job *job = parse_command_line(line);
        if (job) {
            launch_job(job);
        }
    }

    return 0;
}
