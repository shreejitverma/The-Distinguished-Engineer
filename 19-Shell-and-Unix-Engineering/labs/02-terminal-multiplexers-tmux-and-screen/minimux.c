/*
 * minimux: the smallest terminal multiplexer that still shows the mechanism of tmux and screen.
 *
 * A server process holds the master side of one pseudo-terminal and runs one program (the pane) on
 * the slave side. Clients come and go over a Unix-domain socket; the pane never notices, because
 * the terminal it talks to belongs to the server, not to any client.
 *
 * Usage:
 *   minimux server [-p smallest|latest] SOCK CMD [ARG...]
 *       Listen on SOCK, start CMD on a new pty in a new session, print "server_pid=N", and return
 *       while the server keeps running in the background with no controlling terminal.
 *       -p chooses how the single pty size follows several clients: the minimum of every attached
 *       client in each dimension (smallest, the default), or the client that attached, typed, or
 *       resized most recently (latest). With no client attached the size stays as it was;
 *       a new pane starts at 24 rows by 80 columns.
 *   minimux drive SOCK STEP...
 *       A scripted set of clients. Each STEP is NAME:VERB or NAME:VERB=ARG, NAME one letter a to h:
 *         attach=ROWSxCOLS  connect NAME and attach it with that terminal size
 *         send=TEXT         type TEXT (escapes: \n \r \\ \ooo) into the pane
 *         expect=TEXT       wait until NAME has received a complete line containing TEXT
 *         resize=ROWSxCOLS  tell the server NAME's terminal changed size
 *         await=KEY=VALUE   wait until the server's state has that field (KEY!=VALUE: differs)
 *         close             disconnect NAME, as a closed terminal or a dropped ssh link would
 *         closed            wait until the server ends NAME's connection
 *       Afterwards prints what each client received, one "NAME: line" per pane line (carriage
 *       returns removed), then "NAME: [end: REASON]". Exit status 3 when a wait times out.
 *   minimux info SOCK [KEY...]
 *       Print the server's state (only the named fields, when given) as KEY=VALUE words, or
 *       "connect=ERRNO" with exit status 1 when no server answers on SOCK.
 *   minimux wait SOCK KEY=VALUE
 *       Poll the server until its state has that field; exit status 1 on timeout.
 *   minimux kill-server SOCK
 *       Tell the server to exit. It removes its socket and closes the pty master, which hangs up
 *       the pane.
 *   minimux ids
 *       Run inside a pane: is stdin a terminal, is the parent (the pane's shell) the session
 *       leader, is the parent's process group the terminal's foreground group, does /dev/tty open.
 *   minimux hex STRING
 *       Print "hex:" and STRING's bytes in hexadecimal.
 *   minimux limits
 *       Print sizeof(sun_path), the limit on a socket path.
 *
 * Unlike tmux and screen, minimux does not interpret the pane's output: it relays the bytes and
 * replays the last 64 KiB to a new client, where a real multiplexer keeps a parsed screen and
 * redraws it. The clients relay bytes through the socket; tmux and screen instead hand the
 * server the client's terminal itself.
 *
 * Every wait is bounded, and a server ends itself after SERVER_LIFETIME seconds, so a failed
 * test cannot leave one behind.
 */
#define _GNU_SOURCE /* glibc declares posix_openpt, grantpt, unlockpt, and ptsname only on request */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define MAX_CLIENTS 8
#define HISTORY_CAP 65536
#define FRAME_MAX 4096
#define SERVER_LIFETIME 60 /* seconds */
#define WAIT_SECONDS 20
#define PREFIX_KEY 0x02 /* Control-B, the tmux default; screen uses Control-A (0x01) */

/* Frame types. Client to server: */
#define F_ATTACH 'A' /* payload "ROWS COLS" */
#define F_INPUT 'I'  /* payload: keystrokes */
#define F_RESIZE 'W' /* payload "ROWS COLS" */
#define F_QUERY 'Q'  /* no payload */
#define F_KILL 'K'   /* no payload: the server exits */
/* Server to client: */
#define F_OUTPUT 'O'   /* payload: bytes the pane wrote */
#define F_STATE 'S'    /* payload: the state line */
#define F_DETACHED 'D' /* the server detached this client (prefix d) */
#define F_EXITED 'X'   /* payload: the pane's exit status in decimal */

static const char *errname(int e) {
  switch (e) {
  case ENOENT:
    return "ENOENT";
  case ECONNREFUSED:
    return "ECONNREFUSED";
  case EACCES:
    return "EACCES";
  case ENAMETOOLONG:
    return "ENAMETOOLONG";
  case EADDRINUSE:
    return "EADDRINUSE";
  default:
    return "other";
  }
}

static long now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int write_all(int fd, const void *buf, size_t n) {
  const char *p = buf;
  while (n > 0) {
    ssize_t w = write(fd, p, n);
    if (w < 0) {
      if (errno == EINTR)
        continue;
      return -1;
    }
    p += w;
    n -= (size_t)w;
  }
  return 0;
}

/* One frame is a type byte, a two-byte big-endian length, and that many payload bytes. */
static int send_frame(int fd, char type, const void *payload, size_t n) {
  unsigned char buf[3 + FRAME_MAX];
  if (n > FRAME_MAX)
    n = FRAME_MAX;
  buf[0] = (unsigned char)type;
  buf[1] = (unsigned char)(n >> 8);
  buf[2] = (unsigned char)(n & 0xff);
  if (n > 0)
    memcpy(buf + 3, payload, n);
  return write_all(fd, buf, 3 + n);
}

/* A receive buffer that hands out complete frames. */
struct inbuf {
  unsigned char data[2 * (3 + FRAME_MAX)];
  size_t len;
};

/* Read once from FD into IN. Returns bytes read, 0 at end of file, -1 on error. */
static ssize_t inbuf_fill(struct inbuf *in, int fd) {
  ssize_t n = read(fd, in->data + in->len, sizeof in->data - in->len);
  if (n > 0)
    in->len += (size_t)n;
  return n;
}

/* Take the next complete frame out of IN. Returns 1 and sets TYPE, PAYLOAD (nul-terminated), N. */
static int inbuf_next(struct inbuf *in, char *type, unsigned char *payload, size_t *n) {
  if (in->len < 3)
    return 0;
  size_t want = ((size_t)in->data[1] << 8) | in->data[2];
  if (want > FRAME_MAX)
    want = FRAME_MAX;
  if (in->len < 3 + want)
    return 0;
  *type = (char)in->data[0];
  memcpy(payload, in->data + 3, want);
  payload[want] = '\0';
  *n = want;
  memmove(in->data, in->data + 3 + want, in->len - 3 - want);
  in->len -= 3 + want;
  return 1;
}

/*
 * Fill ADDR for the socket PATH and change to its directory, so the address holds only the last
 * component: sun_path is 104 bytes on macOS and 108 on Linux, shorter than many real paths.
 * Returns a descriptor for the previous directory (restore it with leave_dir), or -1 with errno.
 */
static int enter_socket_dir(const char *path, struct sockaddr_un *addr) {
  char dir[4096];
  const char *base = strrchr(path, '/');
  int back = open(".", O_RDONLY);
  if (back < 0)
    return -1;
  if (base != NULL) {
    size_t dlen = (size_t)(base - path);
    if (dlen == 0)
      dlen = 1; /* the socket is directly under "/" */
    if (dlen >= sizeof dir) {
      close(back);
      errno = ENAMETOOLONG;
      return -1;
    }
    memcpy(dir, path, dlen);
    dir[dlen] = '\0';
    if (chdir(dir) != 0) {
      int saved = errno;
      close(back);
      errno = saved;
      return -1;
    }
    base++;
  } else {
    base = path;
  }
  memset(addr, 0, sizeof *addr);
  addr->sun_family = AF_UNIX;
  if (strlen(base) >= sizeof addr->sun_path) { /* the same test tmux makes before bind and connect */
    if (fchdir(back) != 0) {
      /* nothing more can be done; the caller reports ENAMETOOLONG */
    }
    close(back);
    errno = ENAMETOOLONG;
    return -1;
  }
  strcpy(addr->sun_path, base);
  return back;
}

static void leave_dir(int back) {
  int saved = errno;
  if (fchdir(back) != 0) {
    /* the previous directory is gone; relative paths would fail later and say so */
  }
  close(back);
  errno = saved;
}

/* Connect to the server on PATH. Returns the descriptor, or -1 with errno set. */
static int connect_to(const char *path) {
  struct sockaddr_un addr;
  int back = enter_socket_dir(path, &addr);
  if (back < 0)
    return -1;
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd >= 0 && connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
    int saved = errno;
    close(fd);
    fd = -1;
    errno = saved;
  }
  leave_dir(back);
  return fd;
}

static int parse_size(const char *s, int *rows, int *cols) {
  char sep;
  return sscanf(s, "%d%c%d", rows, &sep, cols) == 3 && (sep == 'x' || sep == ' ') && *rows > 0 && *cols > 0;
}

/* ------------------------------------------------------------------ server */

struct client {
  int fd; /* -1 when the slot is free */
  int attached;
  int rows, cols;
  unsigned long active; /* larger means more recently attached, typed, or resized */
  int prefix_pending;   /* the previous key was the prefix */
  struct inbuf in;
};

static struct client clients[MAX_CLIENTS];
static int master = -1;
static int policy_latest;
static int cur_rows = 24, cur_cols = 80;
static unsigned long activity;
static char history[HISTORY_CAP];
static size_t history_len;
static unsigned long detached_bytes; /* pane output that arrived while no client was attached */
static pid_t pane_pid;
static int kill_requested;

static int attached_count(void) {
  int n = 0;
  for (int i = 0; i < MAX_CLIENTS; i++)
    if (clients[i].fd >= 0 && clients[i].attached)
      n++;
  return n;
}

static void drop_client(struct client *c) {
  close(c->fd);
  c->fd = -1;
  c->attached = 0;
}

/* The pty has exactly one size, so several clients force a policy. */
static void apply_size(void) {
  int rows = 0, cols = 0;
  const struct client *latest = NULL;
  for (int i = 0; i < MAX_CLIENTS; i++) {
    const struct client *c = &clients[i];
    if (c->fd < 0 || !c->attached)
      continue;
    if (latest == NULL || c->active > latest->active)
      latest = c;
    if (rows == 0 || c->rows < rows)
      rows = c->rows;
    if (cols == 0 || c->cols < cols)
      cols = c->cols;
  }
  if (latest == NULL)
    return; /* nobody attached: the pane keeps its size */
  if (policy_latest) {
    rows = latest->rows;
    cols = latest->cols;
  }
  if (rows == cur_rows && cols == cur_cols)
    return;
  struct winsize ws;
  memset(&ws, 0, sizeof ws);
  ws.ws_row = (unsigned short)rows;
  ws.ws_col = (unsigned short)cols;
  if (ioctl(master, TIOCSWINSZ, &ws) == 0) { /* the kernel sends SIGWINCH to the pane's foreground group */
    cur_rows = rows;
    cur_cols = cols;
  }
}

static void remember(const char *buf, size_t n) {
  if (n >= HISTORY_CAP) {
    buf += n - HISTORY_CAP;
    n = HISTORY_CAP;
  }
  if (history_len + n > HISTORY_CAP) {
    size_t drop = history_len + n - HISTORY_CAP;
    memmove(history, history + drop, history_len - drop);
    history_len -= drop;
  }
  memcpy(history + history_len, buf, n);
  history_len += n;
}

/* Pane output: keep it, and copy it to every attached client. */
static void broadcast(const char *buf, size_t n) {
  int sent = 0;
  remember(buf, n);
  for (int i = 0; i < MAX_CLIENTS; i++) {
    struct client *c = &clients[i];
    if (c->fd < 0 || !c->attached)
      continue;
    if (send_frame(c->fd, F_OUTPUT, buf, n) != 0)
      drop_client(c);
    else
      sent++;
  }
  if (sent == 0)
    detached_bytes += n;
}

/* Keystrokes from one client: the server consumes the prefix key and what follows it. */
static void client_input(struct client *c, const unsigned char *buf, size_t n) {
  unsigned char out[FRAME_MAX];
  size_t len = 0;
  int detach = 0;
  for (size_t i = 0; i < n && !detach; i++) {
    unsigned char b = buf[i];
    if (c->prefix_pending) {
      c->prefix_pending = 0;
      if (b == PREFIX_KEY)
        out[len++] = b; /* prefix twice: one literal prefix byte for the pane */
      else if (b == 'd')
        detach = 1; /* prefix d: detach this client */
      /* any other key after the prefix is unbound and is dropped */
    } else if (b == PREFIX_KEY) {
      c->prefix_pending = 1;
    } else {
      out[len++] = b;
    }
  }
  if (len > 0 && write_all(master, out, len) != 0) {
    /* the pane is gone; the main loop notices on its next pass */
  }
  if (detach) {
    if (send_frame(c->fd, F_DETACHED, NULL, 0) != 0) {
      /* the client left first; it is dropped either way */
    }
    drop_client(c);
  }
}

static void send_state(struct client *c) {
  char line[512];
  int tty = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (tty >= 0)
    close(tty);
  int n = snprintf(line, sizeof line,
                   "server_pid=%ld pane_pid=%ld clients=%d rows=%d cols=%d policy=%s history=%zu "
                   "detached_bytes=%lu server_tty=%d server_leader=%d",
                   (long)getpid(), (long)pane_pid, attached_count(), cur_rows, cur_cols,
                   policy_latest ? "latest" : "smallest", history_len, detached_bytes, tty >= 0,
                   getsid(0) == getpid());
  if (send_frame(c->fd, F_STATE, line, (size_t)n) != 0)
    drop_client(c);
}

static void client_frames(struct client *c) {
  char type;
  unsigned char payload[FRAME_MAX + 1];
  size_t n;
  while (c->fd >= 0 && inbuf_next(&c->in, &type, payload, &n)) {
    int rows, cols;
    switch (type) {
    case F_ATTACH:
      if (!parse_size((char *)payload, &rows, &cols))
        break;
      c->attached = 1;
      c->rows = rows;
      c->cols = cols;
      c->active = ++activity;
      apply_size();
      for (size_t off = 0; off < history_len && c->fd >= 0; off += FRAME_MAX) {
        size_t part = history_len - off < FRAME_MAX ? history_len - off : FRAME_MAX;
        if (send_frame(c->fd, F_OUTPUT, history + off, part) != 0)
          drop_client(c);
      }
      break;
    case F_RESIZE:
      if (!c->attached || !parse_size((char *)payload, &rows, &cols))
        break;
      c->rows = rows;
      c->cols = cols;
      c->active = ++activity;
      apply_size();
      break;
    case F_INPUT:
      if (!c->attached)
        break;
      c->active = ++activity;
      apply_size(); /* under "latest", typing makes this client the one the size follows */
      client_input(c, payload, n);
      break;
    case F_QUERY:
      send_state(c);
      break;
    case F_KILL:
      kill_requested = 1;
      break;
    default:
      break;
    }
  }
}

/* The pane: a new session whose controlling terminal is the slave side. */
static void pane_exec(const char *slave_name, const char *sock, int ready, char **argv) {
  struct winsize ws;
  if (setsid() < 0)
    _exit(126);
  int slave = open(slave_name, O_RDWR); /* on Linux this open already makes it the controlling terminal */
  if (slave < 0)
    _exit(126);
  if (ioctl(slave, TIOCSCTTY, 0) != 0) /* BSD and macOS need the explicit request */
    _exit(126);
  /* macOS refuses TIOCSWINSZ on a master whose slave is not open yet, so the first size goes through the slave. */
  memset(&ws, 0, sizeof ws);
  ws.ws_row = (unsigned short)cur_rows;
  ws.ws_col = (unsigned short)cur_cols;
  if (ioctl(slave, TIOCSWINSZ, &ws) != 0 || write(ready, "1", 1) != 1)
    _exit(126);
  if (dup2(slave, 0) < 0 || dup2(slave, 1) < 0 || dup2(slave, 2) < 0)
    _exit(126);
  for (int fd = 3; fd < 256; fd++)
    close(fd);
  signal(SIGPIPE, SIG_DFL);
  setenv("MINIMUX", sock, 1); /* how a program learns it is inside the multiplexer, like TMUX and STY */
  execvp(argv[0], argv);
  _exit(127);
}

static int cmd_server(int argc, char **argv) {
  int arg = 0;
  if (argc >= 2 && strcmp(argv[0], "-p") == 0) {
    if (strcmp(argv[1], "latest") == 0) {
      policy_latest = 1;
    } else if (strcmp(argv[1], "smallest") != 0) {
      fprintf(stderr, "minimux: unknown policy %s\n", argv[1]);
      return 2;
    }
    arg = 2;
  }
  if (argc - arg < 2) {
    fprintf(stderr, "usage: minimux server [-p smallest|latest] SOCK CMD [ARG...]\n");
    return 2;
  }
  const char *sock = argv[arg];
  char **cmd = argv + arg + 1;

  /* A socket file with no server behind it is stale: remove it. A live server keeps the path. */
  int probe = connect_to(sock);
  if (probe >= 0) {
    close(probe);
    printf("bind=%s\n", errname(EADDRINUSE));
    return 1;
  }
  int stale = errno == ECONNREFUSED;

  struct sockaddr_un addr;
  int back = enter_socket_dir(sock, &addr);
  if (back < 0) {
    printf("bind=%s\n", errname(errno));
    return 1;
  }
  if (stale)
    unlink(addr.sun_path);
  int listener = socket(AF_UNIX, SOCK_STREAM, 0);
  mode_t old = umask(S_IXUSR | S_IRWXG | S_IRWXO); /* the socket is created rw for its owner only */
  int rc = listener < 0 ? -1 : bind(listener, (struct sockaddr *)&addr, sizeof addr);
  int saved = errno;
  umask(old);
  leave_dir(back);
  if (rc != 0 || listen(listener, 16) != 0) {
    printf("bind=%s\n", errname(rc != 0 ? saved : errno));
    return 1;
  }

  fflush(stdout);
  pid_t pid = fork();
  if (pid < 0)
    return 1;
  if (pid > 0) {
    printf("server_pid=%ld\n", (long)pid);
    return 0; /* the listener already exists, so a client can connect as soon as we return */
  }

  /* The server: its own session, no controlling terminal, nothing shared with whoever started it. */
  if (setsid() < 0)
    _exit(1);
  int devnull = open("/dev/null", O_RDWR);
  if (devnull < 0 || dup2(devnull, 0) < 0 || dup2(devnull, 1) < 0 || dup2(devnull, 2) < 0)
    _exit(1);
  for (int fd = 3; fd < 256; fd++)
    if (fd != listener)
      close(fd);
  signal(SIGPIPE, SIG_IGN); /* a client that vanished must not kill the server */
  for (int i = 0; i < MAX_CLIENTS; i++)
    clients[i].fd = -1;

  master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt(master) != 0 || unlockpt(master) != 0)
    _exit(1);
  const char *slave_name = ptsname(master);
  if (slave_name == NULL)
    _exit(1);
  int ready[2];
  if (pipe(ready) != 0)
    _exit(1);
  pane_pid = fork();
  if (pane_pid < 0)
    _exit(1);
  if (pane_pid == 0)
    pane_exec(slave_name, sock, ready[1], cmd);
  /* Serve nobody until the pane has opened its terminal: a resize before that would be lost on macOS. */
  close(ready[1]);
  struct pollfd opened = {.fd = ready[0], .events = POLLIN};
  char byte;
  if (poll(&opened, 1, 5000) <= 0 || read(ready[0], &byte, 1) != 1)
    _exit(1);
  close(ready[0]);

  long deadline = now_ms() + SERVER_LIFETIME * 1000L;
  int pane_status = -1, master_open = 1;
  while (now_ms() < deadline && !kill_requested) {
    struct pollfd fds[2 + MAX_CLIENTS];
    int slot[2 + MAX_CLIENTS];
    int nfds = 0;
    fds[nfds].fd = listener;
    fds[nfds].events = POLLIN;
    slot[nfds++] = -1;
    if (master_open) {
      fds[nfds].fd = master;
      fds[nfds].events = POLLIN;
      slot[nfds++] = -2;
    }
    for (int i = 0; i < MAX_CLIENTS; i++) {
      if (clients[i].fd < 0)
        continue;
      fds[nfds].fd = clients[i].fd;
      fds[nfds].events = POLLIN;
      slot[nfds++] = i;
    }
    int reaped_before = pane_status >= 0;
    if (poll(fds, (nfds_t)nfds, 100) < 0 && errno != EINTR)
      break;

    int had_output = 0;
    for (int k = 0; k < nfds; k++) {
      if (fds[k].revents == 0)
        continue;
      if (slot[k] == -1) {
        int fd = accept(listener, NULL, NULL);
        int placed = 0;
        for (int i = 0; fd >= 0 && i < MAX_CLIENTS && !placed; i++) {
          if (clients[i].fd < 0) {
            memset(&clients[i], 0, sizeof clients[i]);
            clients[i].fd = fd;
            placed = 1;
          }
        }
        if (fd >= 0 && !placed)
          close(fd);
      } else if (slot[k] == -2) {
        char buf[FRAME_MAX];
        ssize_t n = read(master, buf, sizeof buf);
        if (n > 0) {
          broadcast(buf, (size_t)n);
          had_output = 1;
        } else if (n == 0 || errno != EINTR) {
          master_open = 0; /* every slave descriptor is closed: 0 on macOS, EIO on Linux */
        }
      } else {
        struct client *c = &clients[slot[k]];
        if (inbuf_fill(&c->in, c->fd) <= 0) {
          drop_client(c); /* the client went away: the pane is told nothing */
          apply_size();
        } else {
          client_frames(c);
        }
      }
    }

    if (pane_status < 0) {
      int st;
      if (waitpid(pane_pid, &st, WNOHANG) == pane_pid)
        pane_status = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
    }
    if (reaped_before && !had_output)
      break; /* the pane exited before this pass and wrote nothing during it: all output is relayed */
  }

  /* Remove the socket first, so nobody connects to a server that is leaving. */
  back = enter_socket_dir(sock, &addr);
  if (back >= 0) {
    unlink(addr.sun_path);
    leave_dir(back);
  }
  char status[16];
  int len = snprintf(status, sizeof status, "%d", pane_status);
  for (int i = 0; i < MAX_CLIENTS; i++) {
    if (clients[i].fd < 0)
      continue;
    if (pane_status >= 0 && send_frame(clients[i].fd, F_EXITED, status, (size_t)len) != 0) {
      /* that client left first */
    }
    close(clients[i].fd);
  }
  _exit(0); /* closing the master hangs up whatever is still running in the pane */
}

/* ------------------------------------------------------------------ clients */

/* Ask the server on PATH for its state line. Returns 0, or -1 with errno set (0 when it hung up). */
static int query_state(const char *path, char *line, size_t cap) {
  int fd = connect_to(path);
  if (fd < 0)
    return -1;
  struct inbuf in;
  in.len = 0;
  char type;
  unsigned char payload[FRAME_MAX + 1];
  size_t n;
  int rc = -1;
  errno = 0;
  if (send_frame(fd, F_QUERY, NULL, 0) == 0) {
    long deadline = now_ms() + 5000;
    while (rc != 0 && now_ms() < deadline) {
      struct pollfd p = {.fd = fd, .events = POLLIN};
      if (poll(&p, 1, 100) <= 0)
        continue;
      if (inbuf_fill(&in, fd) <= 0)
        break;
      while (inbuf_next(&in, &type, payload, &n)) {
        if (type == F_STATE) {
          size_t len = n < cap - 1 ? n : cap - 1;
          memcpy(line, payload, len);
          line[len] = '\0';
          rc = 0;
        }
      }
    }
  }
  int saved = rc == 0 ? 0 : errno;
  close(fd);
  errno = saved;
  return rc;
}

/* Does the state LINE satisfy TOKEN, which is KEY=VALUE or KEY!=VALUE? */
static int state_matches(const char *line, const char *token) {
  char key[64];
  const char *eq = strchr(token, '=');
  if (eq == NULL || eq == token)
    return 0;
  int negate = eq[-1] == '!';
  size_t klen = (size_t)(eq - token) - (negate ? 1 : 0);
  if (klen == 0 || klen + 2 > sizeof key)
    return 0;
  memcpy(key, token, klen);
  key[klen] = '=';
  key[klen + 1] = '\0';
  const char *want = eq + 1;
  for (const char *p = line; *p != '\0';) {
    size_t wlen = strcspn(p, " ");
    if (wlen > klen && strncmp(p, key, klen + 1) == 0) {
      int same = strlen(want) == wlen - klen - 1 && strncmp(p + klen + 1, want, wlen - klen - 1) == 0;
      return negate ? !same : same;
    }
    p += wlen;
    while (*p == ' ')
      p++;
  }
  return 0;
}

static int cmd_info(int argc, char **argv) {
  char line[512];
  if (argc < 1)
    return 2;
  if (query_state(argv[0], line, sizeof line) != 0) {
    printf("connect=%s\n", errno == 0 ? "closed" : errname(errno));
    return 1;
  }
  if (argc == 1) {
    printf("%s\n", line);
    return 0;
  }
  const char *sep = "";
  for (int i = 1; i < argc; i++) {
    size_t klen = strlen(argv[i]);
    for (const char *p = line; *p != '\0';) {
      size_t wlen = strcspn(p, " ");
      if (wlen > klen && p[klen] == '=' && strncmp(p, argv[i], klen) == 0) {
        printf("%s%.*s", sep, (int)wlen, p);
        sep = " ";
      }
      p += wlen;
      while (*p == ' ')
        p++;
    }
  }
  printf("\n");
  return 0;
}

static int cmd_kill_server(const char *path) {
  int fd = connect_to(path);
  if (fd < 0) {
    printf("connect=%s\n", errname(errno));
    return 1;
  }
  char byte;
  int rc = send_frame(fd, F_KILL, NULL, 0);
  struct pollfd p = {.fd = fd, .events = POLLIN};
  if (rc == 0 && poll(&p, 1, 5000) > 0 && read(fd, &byte, 1) != 0)
    rc = -1; /* the server closes the connection, without a reply, once its socket is removed */
  close(fd);
  return rc == 0 ? 0 : 1;
}

static int wait_state(const char *path, const char *token) {
  char line[512] = "";
  long deadline = now_ms() + WAIT_SECONDS * 1000L;
  while (now_ms() < deadline) {
    if (query_state(path, line, sizeof line) == 0 && state_matches(line, token))
      return 0;
    poll(NULL, 0, 20);
  }
  fprintf(stderr, "minimux: timed out waiting for %s; last state: %s\n", token, line);
  return 1;
}

struct peer {
  int used;
  int fd; /* -1 once the connection has ended */
  struct inbuf in;
  char *text; /* everything the pane wrote, as this client received it */
  size_t len, cap, cursor;
  const char *end;
};

static struct peer peers[MAX_CLIENTS];

static void peer_end(struct peer *p, const char *reason) {
  if (p->fd >= 0) {
    close(p->fd);
    p->fd = -1;
  }
  if (p->end == NULL)
    p->end = reason;
}

/* Read whatever the server has sent to any client, waiting at most TIMEOUT_MS for the first byte. */
static void pump(int timeout_ms) {
  static char exited[MAX_CLIENTS][48];
  struct pollfd fds[MAX_CLIENTS];
  int who[MAX_CLIENTS];
  int nfds = 0;
  for (int i = 0; i < MAX_CLIENTS; i++) {
    if (!peers[i].used || peers[i].fd < 0)
      continue;
    fds[nfds].fd = peers[i].fd;
    fds[nfds].events = POLLIN;
    who[nfds++] = i;
  }
  if (nfds == 0) {
    poll(NULL, 0, timeout_ms);
    return;
  }
  if (poll(fds, (nfds_t)nfds, timeout_ms) <= 0)
    return;
  for (int k = 0; k < nfds; k++) {
    struct peer *p = &peers[who[k]];
    char type;
    unsigned char payload[FRAME_MAX + 1];
    size_t n;
    if (fds[k].revents == 0)
      continue;
    ssize_t got = inbuf_fill(&p->in, p->fd);
    while (inbuf_next(&p->in, &type, payload, &n)) {
      if (type == F_OUTPUT) {
        if (p->len + n + 1 > p->cap) {
          p->cap = (p->len + n + 1) * 2;
          p->text = realloc(p->text, p->cap);
          if (p->text == NULL)
            exit(1);
        }
        memcpy(p->text + p->len, payload, n);
        p->len += n;
        p->text[p->len] = '\0';
      } else if (type == F_DETACHED) {
        peer_end(p, "detached by the server");
      } else if (type == F_EXITED) {
        snprintf(exited[who[k]], sizeof exited[0], "pane exited with status %.8s", (char *)payload);
        peer_end(p, exited[who[k]]);
      }
    }
    if (got <= 0)
      peer_end(p, "server gone");
  }
}

/* Decode \n, \r, \\ and \ooo in S into OUT; returns the length. */
static size_t unescape(const char *s, unsigned char *out, size_t cap) {
  size_t len = 0;
  while (*s != '\0' && len < cap) {
    if (*s != '\\') {
      out[len++] = (unsigned char)*s++;
      continue;
    }
    s++;
    if (*s == 'n') {
      out[len++] = '\n';
      s++;
    } else if (*s == 'r') {
      out[len++] = '\r';
      s++;
    } else if (*s >= '0' && *s <= '7') {
      unsigned v = 0;
      for (int d = 0; d < 3 && *s >= '0' && *s <= '7'; d++)
        v = v * 8 + (unsigned)(*s++ - '0');
      out[len++] = (unsigned char)v;
    } else if (*s != '\0') {
      out[len++] = (unsigned char)*s++;
    }
  }
  return len;
}

static int step_fail(const char *step, const struct peer *p) {
  fprintf(stderr, "minimux: step %s failed; received so far: %s\n", step, p->text != NULL ? p->text : "");
  return 3;
}

static int cmd_drive(int argc, char **argv) {
  if (argc < 1)
    return 2;
  const char *sock = argv[0];
  int order[MAX_CLIENTS], norder = 0;
  for (int i = 0; i < MAX_CLIENTS; i++)
    peers[i].fd = -1;

  for (int s = 1; s < argc; s++) {
    const char *step = argv[s];
    if (step[0] < 'a' || step[0] > 'h' || step[1] != ':') {
      fprintf(stderr, "minimux: bad step %s\n", step);
      return 2;
    }
    struct peer *p = &peers[step[0] - 'a'];
    const char *verb = step + 2;
    const char *arg = strchr(verb, '=');
    size_t vlen = arg != NULL ? (size_t)(arg - verb) : strlen(verb);
    arg = arg != NULL ? arg + 1 : "";
    long deadline = now_ms() + WAIT_SECONDS * 1000L;
    int rows, cols;
    char size[32];

    if (vlen == 6 && strncmp(verb, "attach", vlen) == 0) {
      if (!parse_size(arg, &rows, &cols) || p->used)
        return step_fail(step, p);
      p->fd = connect_to(sock);
      if (p->fd < 0) {
        printf("%c: connect=%s\n", step[0], errname(errno));
        return 1;
      }
      p->used = 1;
      order[norder++] = step[0] - 'a';
      int n = snprintf(size, sizeof size, "%d %d", rows, cols);
      if (send_frame(p->fd, F_ATTACH, size, (size_t)n) != 0)
        return step_fail(step, p);
    } else if (vlen == 4 && strncmp(verb, "send", vlen) == 0) {
      unsigned char bytes[FRAME_MAX];
      size_t n = unescape(arg, bytes, sizeof bytes);
      if (p->fd < 0 || send_frame(p->fd, F_INPUT, bytes, n) != 0)
        return step_fail(step, p);
    } else if (vlen == 6 && strncmp(verb, "resize", vlen) == 0) {
      if (p->fd < 0 || !parse_size(arg, &rows, &cols))
        return step_fail(step, p);
      int n = snprintf(size, sizeof size, "%d %d", rows, cols);
      if (send_frame(p->fd, F_RESIZE, size, (size_t)n) != 0)
        return step_fail(step, p);
    } else if (vlen == 6 && strncmp(verb, "expect", vlen) == 0) {
      /* Wait for a complete line, after the last one matched, that contains ARG. */
      for (;;) {
        const char *hit = p->text != NULL ? strstr(p->text + p->cursor, arg) : NULL;
        const char *eol = hit != NULL ? strchr(hit + strlen(arg), '\n') : NULL;
        if (eol != NULL) {
          p->cursor = (size_t)(eol - p->text) + 1;
          break;
        }
        if (p->fd < 0 || now_ms() >= deadline)
          return step_fail(step, p);
        pump(50);
      }
    } else if (vlen == 5 && strncmp(verb, "await", vlen) == 0) {
      char line[512] = "";
      for (;;) {
        if (query_state(sock, line, sizeof line) == 0 && state_matches(line, arg))
          break;
        if (now_ms() >= deadline) {
          fprintf(stderr, "minimux: last state: %s\n", line);
          return step_fail(step, p);
        }
        pump(20);
      }
    } else if (vlen == 5 && strncmp(verb, "close", vlen) == 0) {
      if (p->fd < 0)
        return step_fail(step, p);
      peer_end(p, "closed by the client");
    } else if (vlen == 6 && strncmp(verb, "closed", vlen) == 0) {
      while (p->fd >= 0) {
        if (now_ms() >= deadline)
          return step_fail(step, p);
        pump(50);
      }
    } else {
      fprintf(stderr, "minimux: bad step %s\n", step);
      return 2;
    }
  }

  pump(0);
  for (int k = 0; k < norder; k++) {
    struct peer *p = &peers[order[k]];
    char name = (char)('a' + order[k]);
    const char *line = p->text != NULL ? p->text : "";
    while (*line != '\0') {
      size_t n = strcspn(line, "\n");
      printf("%c: ", name);
      for (size_t i = 0; i < n; i++)
        if (line[i] != '\r')
          putchar(line[i]);
      putchar('\n');
      line += n;
      if (*line == '\n')
        line++;
    }
    if (p->fd >= 0)
      peer_end(p, "closed by the client");
    printf("%c: [end: %s]\n", name, p->end);
  }
  return 0;
}

/* ------------------------------------------------------------------ probes run inside a pane */

static int cmd_ids(void) {
  int tty = open("/dev/tty", O_RDWR | O_NOCTTY);
  if (tty >= 0)
    close(tty);
  printf("stdin_tty=%d shell_is_leader=%d fg_is_shell_group=%d dev_tty=%d\n", isatty(0), getsid(0) == getppid(),
         tcgetpgrp(0) == getpgid(getppid()), tty >= 0);
  return 0;
}

static int cmd_hex(const char *s) {
  printf("hex:");
  for (; *s != '\0'; s++)
    printf("%02x", (unsigned char)*s);
  printf("\n");
  return 0;
}

int main(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "server") == 0)
    return cmd_server(argc - 2, argv + 2);
  if (argc >= 2 && strcmp(argv[1], "drive") == 0)
    return cmd_drive(argc - 2, argv + 2);
  if (argc >= 2 && strcmp(argv[1], "info") == 0)
    return cmd_info(argc - 2, argv + 2);
  if (argc == 4 && strcmp(argv[1], "wait") == 0)
    return wait_state(argv[2], argv[3]);
  if (argc == 3 && strcmp(argv[1], "kill-server") == 0)
    return cmd_kill_server(argv[2]);
  if (argc == 2 && strcmp(argv[1], "ids") == 0)
    return cmd_ids();
  if (argc == 3 && strcmp(argv[1], "hex") == 0)
    return cmd_hex(argv[2]);
  if (argc == 2 && strcmp(argv[1], "limits") == 0) {
    printf("sun_path=%zu\n", sizeof(((struct sockaddr_un *)0)->sun_path));
    return 0;
  }
  fprintf(stderr, "usage: minimux server|drive|info|wait|kill-server|ids|hex|limits ... (see the comment at the top of minimux.c)\n");
  return 2;
}
