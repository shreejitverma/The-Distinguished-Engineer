/*
 * redos-steps.c - count the work two matching strategies do on the same input.
 *
 * Usage: redos-steps MODE PATTERN UNIT COUNT SUFFIX [LIMIT]
 *   The subject is UNIT repeated COUNT times, then SUFFIX.
 *   MODE is one of:
 *     bt           recursive backtracking over a Thompson NFA (Perl, PCRE, Python re style)
 *     nfa          Thompson's parallel NFA simulation (RE2, Rust regex, awk, grep style)
 *     glob-naive   recursive shell-pattern matcher that retries every earlier star
 *                  (dash pmatch, mksh and posh do_gmatch style)
 *     glob-linear  shell-pattern matcher that never backtracks past the last star
 *                  (Cox 2017; musl fnmatch, bash 5 "ends" style)
 *   Regex modes accept literals, '.', '(', ')', '|', '*', '+', '?' and match the
 *   whole subject (like ^...$). Glob modes accept literals, '?', and '*'.
 *   LIMIT caps the steps (default 100000000); bt also caps recursion depth at 10000,
 *   the way PCRE2's match and depth limits do.
 *
 * Prints: steps=N result=match|nomatch|limit|depth
 * Exit status: 0 match, 1 no match, 3 limit or depth reached, 2 usage error.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CHAR, ANY, SPLIT, NOP, MATCH };

struct state {
  int op;
  int c;
  int out;
  int out1;
};

struct frag {
  int start;
  int end; /* a NOP state whose out is still unpatched */
};

#define MAXSTATES 4096
#define MAXDEPTH 10000

static struct state st[MAXSTATES];
static int nstates;
static const char *pat;
static int perr;

static const char *subj;
static size_t n;
static unsigned long steps;
static unsigned long limit = 100000000UL;
static int over; /* 1 = step limit, 2 = depth limit */

static int newstate(int op, int c, int out, int out1)
{
  if (nstates >= MAXSTATES) {
    perr = 1;
    return 0;
  }
  st[nstates].op = op;
  st[nstates].c = c;
  st[nstates].out = out;
  st[nstates].out1 = out1;
  return nstates++;
}

static struct frag parse_alt(void);

static struct frag parse_atom(void)
{
  struct frag f;
  int e;

  if (*pat == '(') {
    pat++;
    f = parse_alt();
    if (*pat != ')')
      perr = 1;
    else
      pat++;
    return f;
  }
  if (*pat == '\0' || *pat == '|' || *pat == ')' || *pat == '*' || *pat == '+' || *pat == '?') {
    perr = 1;
    f.start = f.end = newstate(NOP, 0, -1, -1);
    return f;
  }
  e = newstate(NOP, 0, -1, -1);
  if (*pat == '.')
    f.start = newstate(ANY, 0, e, -1);
  else
    f.start = newstate(CHAR, (unsigned char)*pat, e, -1);
  f.end = e;
  pat++;
  return f;
}

static struct frag parse_rep(void)
{
  struct frag f = parse_atom();
  int s, e;

  while (*pat == '*' || *pat == '+' || *pat == '?') {
    e = newstate(NOP, 0, -1, -1);
    s = newstate(SPLIT, 0, f.start, e); /* greedy: try the body first */
    if (*pat == '?')
      st[f.end].out = e;
    else
      st[f.end].out = s; /* loop back for '*' and '+' */
    if (*pat != '+')
      f.start = s; /* '+' must run the body once before the split */
    f.end = e;
    pat++;
  }
  return f;
}

static struct frag parse_cat(void)
{
  struct frag f, g;

  if (*pat == '\0' || *pat == '|' || *pat == ')') {
    f.start = f.end = newstate(NOP, 0, -1, -1);
    return f;
  }
  f = parse_rep();
  while (*pat != '\0' && *pat != '|' && *pat != ')') {
    g = parse_rep();
    st[f.end].out = g.start;
    f.end = g.end;
  }
  return f;
}

static struct frag parse_alt(void)
{
  struct frag f = parse_cat(), g;
  int s, e;

  while (*pat == '|') {
    pat++;
    g = parse_cat();
    e = newstate(NOP, 0, -1, -1);
    s = newstate(SPLIT, 0, f.start, g.start);
    st[f.end].out = e;
    st[g.end].out = e;
    f.start = s;
    f.end = e;
  }
  return f;
}

/* Depth-first search: one path at a time, retrying the other branch on failure. */
static int bt(int s, size_t i, int depth)
{
  if (depth > MAXDEPTH) {
    over = 2;
    return 0;
  }
  for (;;) {
    if (++steps > limit) {
      over = 1;
      return 0;
    }
    switch (st[s].op) {
    case CHAR:
      if (i < n && (unsigned char)subj[i] == st[s].c) {
        s = st[s].out;
        i++;
        continue;
      }
      return 0;
    case ANY:
      if (i < n) {
        s = st[s].out;
        i++;
        continue;
      }
      return 0;
    case NOP:
      s = st[s].out;
      continue;
    case SPLIT:
      if (bt(st[s].out, i, depth + 1))
        return 1;
      if (over)
        return 0;
      s = st[s].out1;
      continue;
    case MATCH:
      return i == n;
    default:
      return 0;
    }
  }
}

static int *mark;
static int markgen;

static void addstate(int *list, int *len, int s)
{
  if (mark[s] == markgen)
    return;
  mark[s] = markgen;
  steps++;
  if (st[s].op == SPLIT) {
    addstate(list, len, st[s].out);
    addstate(list, len, st[s].out1);
  } else if (st[s].op == NOP) {
    addstate(list, len, st[s].out);
  } else {
    list[(*len)++] = s;
  }
}

/* Thompson simulation: every live state advances in lock step, each at most once per byte. */
static int nfa(int start)
{
  int *clist = malloc(sizeof *clist * (size_t)nstates);
  int *nlist = malloc(sizeof *nlist * (size_t)nstates);
  int *tmp, clen = 0, nlen, k, s, matched = 0;
  size_t i;

  mark = calloc((size_t)nstates, sizeof *mark);
  if (clist == NULL || nlist == NULL || mark == NULL) {
    fprintf(stderr, "redos-steps: out of memory\n");
    exit(2);
  }
  markgen = 1;
  addstate(clist, &clen, start);
  for (i = 0; i < n && clen > 0; i++) {
    markgen++;
    nlen = 0;
    for (k = 0; k < clen; k++) {
      s = clist[k];
      steps++;
      if ((st[s].op == CHAR && (unsigned char)subj[i] == st[s].c) || st[s].op == ANY)
        addstate(nlist, &nlen, st[s].out);
    }
    tmp = clist;
    clist = nlist;
    nlist = tmp;
    clen = nlen;
  }
  if (i == n)
    for (k = 0; k < clen; k++)
      if (st[clist[k]].op == MATCH)
        matched = 1;
  free(clist);
  free(nlist);
  free(mark);
  return matched;
}

/* Every '*' retries every later split point: cost grows like C(n, stars). */
static int glob_naive(const char *p, const char *s)
{
  for (;;) {
    if (++steps > limit) {
      over = 1;
      return 0;
    }
    if (*p == '*') {
      p++;
      for (;;) {
        if (glob_naive(p, s))
          return 1;
        if (over || *s == '\0')
          return 0;
        s++;
      }
    }
    if (*p == '\0')
      return *s == '\0';
    if (*s == '\0' || (*p != '?' && *p != *s))
      return 0;
    p++;
    s++;
  }
}

/* Remember only the most recent star; an earlier star's choice is never revisited. */
static int glob_linear(const char *p, const char *s)
{
  size_t px = 0, nx = 0, nextpx = 0, nextnx = 0;
  size_t plen = strlen(p), slen = strlen(s);

  while (px < plen || nx < slen) {
    if (++steps > limit) {
      over = 1;
      return 0;
    }
    if (px < plen) {
      if (p[px] == '*') {
        nextpx = px;
        nextnx = nx + 1;
        px++;
        continue;
      }
      if (nx < slen && (p[px] == '?' || p[px] == s[nx])) {
        px++;
        nx++;
        continue;
      }
    }
    if (nextnx > 0 && nextnx <= slen) {
      px = nextpx;
      nx = nextnx;
      continue;
    }
    return 0;
  }
  return 1;
}

int main(int argc, char **argv)
{
  const char *mode, *unit, *suffix;
  char *buf, *end;
  long count;
  size_t ulen, slen, k;
  int matched, start;
  struct frag f;

  if (argc != 6 && argc != 7) {
    fprintf(stderr, "usage: redos-steps bt|nfa|glob-naive|glob-linear PATTERN UNIT COUNT SUFFIX [LIMIT]\n");
    return 2;
  }
  mode = argv[1];
  unit = argv[3];
  suffix = argv[5];
  count = strtol(argv[4], &end, 10);
  if (*end != '\0' || count < 0 || count > 10000000L) {
    fprintf(stderr, "redos-steps: bad COUNT\n");
    return 2;
  }
  if (argc == 7) {
    limit = strtoul(argv[6], &end, 10);
    if (*end != '\0' || limit == 0) {
      fprintf(stderr, "redos-steps: bad LIMIT\n");
      return 2;
    }
  }
  ulen = strlen(unit);
  slen = strlen(suffix);
  n = ulen * (size_t)count + slen;
  buf = malloc(n + 1);
  if (buf == NULL) {
    fprintf(stderr, "redos-steps: out of memory\n");
    return 2;
  }
  for (k = 0; k < (size_t)count; k++)
    memcpy(buf + k * ulen, unit, ulen);
  memcpy(buf + (size_t)count * ulen, suffix, slen + 1);
  subj = buf;

  if (strcmp(mode, "glob-naive") == 0) {
    matched = glob_naive(argv[2], subj);
  } else if (strcmp(mode, "glob-linear") == 0) {
    matched = glob_linear(argv[2], subj);
  } else if (strcmp(mode, "bt") == 0 || strcmp(mode, "nfa") == 0) {
    pat = argv[2];
    f = parse_alt();
    if (perr || *pat != '\0') {
      fprintf(stderr, "redos-steps: bad PATTERN\n");
      return 2;
    }
    st[f.end].out = newstate(MATCH, 0, -1, -1);
    start = f.start;
    if (mode[0] == 'b')
      matched = bt(start, 0, 0);
    else
      matched = nfa(start);
  } else {
    fprintf(stderr, "redos-steps: unknown MODE %s\n", mode);
    return 2;
  }
  free(buf);
  if (over) {
    printf("steps=%lu result=%s\n", steps > limit ? limit : steps, over == 2 ? "depth" : "limit");
    return 3;
  }
  printf("steps=%lu result=%s\n", steps, matched ? "match" : "nomatch");
  return matched ? 0 : 1;
}
