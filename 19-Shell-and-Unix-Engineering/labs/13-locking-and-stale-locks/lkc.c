/*
 * lkc - kernel lock probe for the locking lab: flock(2) versus fcntl(2) record locks.
 *
 * Usage: lkc try MODE FILE             exit 0 if an exclusive lock is free, 1 if busy, 2 on error
 *        lkc hold MODE FILE CMD [ARG...] take the lock (non-blocking), then exec CMD holding it
 *        lkc spawn MODE FILE SECONDS   take the lock, fork a child that sleeps SECONDS, print
 *                                      the child's PID, and exit (the parent's copy closes)
 *        lkc close-trap MODE FILE      lock via one descriptor, open and close a second one,
 *                                      and report whether another process then sees it busy
 * MODE is flock (BSD lock on the open file description) or fcntl (POSIX process-owned lock).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int use_fcntl;

static int lock_fd(int fd)
{
	if (use_fcntl) {
		struct flock fl;
		memset(&fl, 0, sizeof fl);
		fl.l_type = F_WRLCK;
		fl.l_whence = SEEK_SET;
		fl.l_start = 0;
		fl.l_len = 0; /* whole file */
		return fcntl(fd, F_SETLK, &fl);
	}
	return flock(fd, LOCK_EX | LOCK_NB);
}

static int is_busy(int e)
{
	/* fcntl may report a conflict as EACCES or EAGAIN; flock reports EWOULDBLOCK. */
	return e == EWOULDBLOCK || e == EAGAIN || e == EACCES;
}

static int open_lock(const char *path)
{
	/* O_RDWR because an fcntl write lock needs a descriptor open for writing. */
	return open(path, O_RDWR | O_CREAT, 0644);
}

/* 0 acquired (and released again on close), 1 busy, 2 error. */
static int try_lock(const char *path)
{
	int fd = open_lock(path);
	if (fd < 0) {
		perror(path);
		return 2;
	}
	if (lock_fd(fd) == 0) {
		close(fd);
		return 0;
	}
	if (is_busy(errno)) {
		close(fd);
		return 1;
	}
	perror("lock");
	close(fd);
	return 2;
}

/* Ask a separate process, with its own descriptor, whether the lock is free. */
static int probe_from_child(const char *path)
{
	int st;
	pid_t c = fork();
	if (c < 0)
		return 2;
	if (c == 0)
		_exit(try_lock(path));
	if (waitpid(c, &st, 0) < 0)
		return 2;
	return WIFEXITED(st) ? WEXITSTATUS(st) : 2;
}

static const char *word(int r)
{
	return r == 0 ? "free" : r == 1 ? "busy" : "error";
}

static int take(const char *path)
{
	int fd = open_lock(path);
	if (fd < 0) {
		perror(path);
		return -1;
	}
	if (lock_fd(fd) != 0) {
		perror("lock");
		close(fd);
		return -1;
	}
	return fd;
}

int main(int argc, char **argv)
{
	int fd;
	if (argc < 4)
		goto usage;
	if (strcmp(argv[2], "fcntl") == 0)
		use_fcntl = 1;
	else if (strcmp(argv[2], "flock") != 0)
		goto usage;

	if (strcmp(argv[1], "try") == 0)
		return try_lock(argv[3]);

	if (strcmp(argv[1], "hold") == 0 && argc >= 5) {
		if (take(argv[3]) < 0)
			return 1;
		/* The descriptor has no FD_CLOEXEC, so CMD inherits it and both lock kinds survive execve. */
		execvp(argv[4], argv + 4);
		perror(argv[4]);
		return 2;
	}

	if (strcmp(argv[1], "spawn") == 0 && argc == 5) {
		pid_t c;
		int secs = atoi(argv[4]);
		if (take(argv[3]) < 0)
			return 1;
		c = fork();
		if (c < 0) {
			perror("fork");
			return 2;
		}
		if (c == 0) {
			/* Detach from the caller's pipes so a command substitution does not wait for us. */
			int nul = open("/dev/null", O_RDWR);
			if (nul < 0 || dup2(nul, 0) < 0 || dup2(nul, 1) < 0 || dup2(nul, 2) < 0)
				_exit(3);
			sleep((unsigned)secs);
			_exit(0);
		}
		printf("%ld\n", (long)c);
		return 0;
	}

	if (strcmp(argv[1], "close-trap") == 0) {
		int fd2, before, after;
		fd = take(argv[3]);
		if (fd < 0)
			return 2;
		fd2 = open(argv[3], O_RDONLY);
		if (fd2 < 0) {
			perror(argv[3]);
			return 2;
		}
		before = probe_from_child(argv[3]);
		close(fd2); /* an unrelated descriptor for the same file, as a library might open */
		after = probe_from_child(argv[3]);
		printf("before_close=%s after_close=%s\n", word(before), word(after));
		close(fd);
		return 0;
	}

usage:
	fprintf(stderr, "usage: lkc try|hold|spawn|close-trap flock|fcntl FILE [ARGS]\n");
	return 2;
}
