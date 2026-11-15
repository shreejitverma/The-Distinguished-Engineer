/*
 * engines.c - four matchers over one Thompson NFA, each with a step counter.
 *
 * Usage (the subject is read from stdin; one trailing newline is dropped):
 *   engines backtrack PATTERN [LIMIT]  recursive backtracking, leftmost-first
 *   engines memo PATTERN               backtracking with a visited (state, pos) bitmap
 *   engines thompson PATTERN           Thompson simulation of the state set
 *   engines dfa PATTERN CAP            lazy DFA whose cache holds at most CAP states
 *   engines gen N                      print N pseudo-random a/b bytes and a newline
 *
 * The syntax is literals, '.', '|', '*', '+', '?', '(', ')', and '\c' for a literal c.
 * Every match is anchored at the start of the subject (a prefix match), as with '^'.
 * A loop whose body can match the empty string is rejected, so the NFA has no
 * cycle of empty moves and the plain backtracker always terminates.
 *
 * Output: one line "engine=E nfa=M match=0|1 end=K steps=S", plus
 * "states=B flushes=F" for dfa. end is the length of the prefix matched:
 * the backtracker reports the first match its priority order finds (Perl
 * semantics); thompson and dfa report the longest (the POSIX rule for the
 * whole match). Exit status: 0 match, 1 no match, 2 usage or syntax error,
 * 3 backtrack step limit exceeded (the analogue of PCRE2_ERROR_MATCHLIMIT).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CHAR, ANY, SPLIT, MATCH };

typedef struct {
  int op;
  int c;
  int out;
  int out1;
} State;

static State *st;
static int nst;
static int capst;

static const char *subj;
static size_t slen;
static unsigned long long steps;

_Noreturn static void die(const char *msg) {
  fprintf(stderr, "engines: %s\n", msg);
  exit(2);
}

static void *xrealloc(void *p, size_t n) {
  void *q = realloc(p, n);
  if (q == NULL) {
    die("out of memory");
  }
  return q;
}

static int newstate(int op, int c, int out, int out1) {
  if (nst == capst) {
    capst = capst ? 2 * capst : 64;
    st = xrealloc(st, (size_t)capst * sizeof *st);
  }
  st[nst].op = op;
  st[nst].c = c;
  st[nst].out = out;
  st[nst].out1 = out1;
  return nst++;
}

/* A fragment: its start state, the dangling exits to patch, and whether it can match "". */
typedef struct {
  int start;
  int *slots; /* slot = 2 * state + (0 for out, 1 for out1) */
  int nslots;
  int nullable;
} Frag;

static Frag frag1(int start, int slot, int nullable) {
  Frag f;
  f.start = start;
  f.slots = xrealloc(NULL, sizeof(int));
  f.slots[0] = slot;
  f.nslots = 1;
  f.nullable = nullable;
  return f;
}

static void patch(Frag *f, int target) {
  int i;
  for (i = 0; i < f->nslots; i++) {
    if (f->slots[i] % 2 == 0) {
      st[f->slots[i] / 2].out = target;
    } else {
      st[f->slots[i] / 2].out1 = target;
    }
  }
  free(f->slots);
  f->slots = NULL;
  f->nslots = 0;
}

static void append(Frag *dst, Frag *src) {
  dst->slots = xrealloc(dst->slots, (size_t)(dst->nslots + src->nslots) * sizeof(int));
  memcpy(dst->slots + dst->nslots, src->slots, (size_t)src->nslots * sizeof(int));
  dst->nslots += src->nslots;
  free(src->slots);
  src->slots = NULL;
  src->nslots = 0;
}

static const char *re;

static Frag parse_alt(void);

static Frag parse_atom(void) {
  Frag f;
  int s;
  if (*re == '(') {
    re++;
    f = parse_alt();
    if (*re != ')') {
      die("missing )");
    }
    re++;
    return f;
  }
  if (*re == '\0' || strchr("|*+?)", *re) != NULL) {
    die("empty operand");
  }
  if (*re == '.') {
    re++;
    s = newstate(ANY, 0, -1, -1);
    return frag1(s, 2 * s, 0);
  }
  if (*re == '\\') {
    re++;
    if (*re == '\0') {
      die("trailing backslash");
    }
  }
  s = newstate(CHAR, (unsigned char)*re, -1, -1);
  re++;
  return frag1(s, 2 * s, 0);
}

static Frag parse_rep(void) {
  Frag f = parse_atom();
  int s;
  while (*re == '*' || *re == '+' || *re == '?') {
    if (*re != '?' && f.nullable) {
      die("loop body can match the empty string");
    }
    s = newstate(SPLIT, 0, f.start, -1);
    if (*re == '*') {
      patch(&f, s);
      f = frag1(s, 2 * s + 1, 1);
    } else if (*re == '+') {
      int start = f.start;
      patch(&f, s);
      f = frag1(start, 2 * s + 1, 0);
    } else {
      Frag g = frag1(s, 2 * s + 1, 1);
      append(&g, &f);
      f = g;
    }
    re++;
  }
  return f;
}

static Frag parse_cat(void) {
  Frag f = parse_rep();
  while (*re != '\0' && *re != '|' && *re != ')') {
    Frag g = parse_rep();
    patch(&f, g.start);
    f.slots = g.slots;
    f.nslots = g.nslots;
    f.nullable = f.nullable && g.nullable;
  }
  return f;
}

static Frag parse_alt(void) {
  Frag f = parse_cat();
  while (*re == '|') {
    Frag g;
    int s;
    re++;
    g = parse_cat();
    s = newstate(SPLIT, 0, f.start, g.start);
    f.start = s;
    append(&f, &g);
    f.nullable = f.nullable || g.nullable;
  }
  return f;
}

static int compile(const char *pattern) {
  Frag f;
  int m;
  re = pattern;
  f = parse_alt();
  if (*re != '\0') {
    die("unbalanced )");
  }
  m = newstate(MATCH, 0, -1, -1);
  patch(&f, m);
  return f.start;
}

/* ---- 1. recursive backtracking: try out before out1, return the first success ---- */

static unsigned long long limit;
static int aborted;

static long bt(int s, size_t i) {
  if (aborted) {
    return -1;
  }
  steps++;
  if (limit != 0 && steps > limit) {
    aborted = 1;
    return -1;
  }
  switch (st[s].op) {
    case CHAR:
      return (i < slen && (unsigned char)subj[i] == st[s].c) ? bt(st[s].out, i + 1) : -1;
    case ANY:
      return i < slen ? bt(st[s].out, i + 1) : -1;
    case SPLIT: {
      long r = bt(st[s].out, i);
      return r >= 0 ? r : bt(st[s].out1, i);
    }
    default:
      return (long)i;
  }
}

/* ---- 2. the same search, pruned by a bitmap of (state, position) pairs already tried ---- */

static unsigned char *visited;

static long memo(int s, size_t i) {
  size_t bit = (size_t)s * (slen + 1) + i;
  if (visited[bit / 8] & (1u << (bit % 8))) {
    return -1; /* tried before and failed: a success would have ended the search */
  }
  visited[bit / 8] |= (unsigned char)(1u << (bit % 8));
  steps++;
  switch (st[s].op) {
    case CHAR:
      return (i < slen && (unsigned char)subj[i] == st[s].c) ? memo(st[s].out, i + 1) : -1;
    case ANY:
      return i < slen ? memo(st[s].out, i + 1) : -1;
    case SPLIT: {
      long r = memo(st[s].out, i);
      return r >= 0 ? r : memo(st[s].out1, i);
    }
    default:
      return (long)i;
  }
}

/* ---- 3. Thompson simulation: advance the whole set of states one byte at a time ---- */

static int *mark;
static int gen;

static void addstate(int *list, int *n, int s) {
  if (mark[s] == gen) {
    return;
  }
  mark[s] = gen;
  steps++;
  if (st[s].op == SPLIT) {
    addstate(list, n, st[s].out);
    addstate(list, n, st[s].out1);
    return;
  }
  list[(*n)++] = s;
}

static long thompson(int start) {
  int *clist = xrealloc(NULL, (size_t)nst * sizeof(int));
  int *nlist = xrealloc(NULL, (size_t)nst * sizeof(int));
  int cn = 0;
  int nn;
  int j;
  long last = -1;
  size_t i;
  mark = calloc((size_t)nst, sizeof(int));
  if (mark == NULL) {
    die("out of memory");
  }
  gen = 1;
  addstate(clist, &cn, start);
  for (i = 0;; i++) {
    for (j = 0; j < cn; j++) {
      if (st[clist[j]].op == MATCH) {
        last = (long)i;
      }
    }
    if (i == slen || cn == 0) {
      break;
    }
    gen++;
    nn = 0;
    for (j = 0; j < cn; j++) {
      State *p = &st[clist[j]];
      if ((p->op == CHAR && p->c == (unsigned char)subj[i]) || p->op == ANY) {
        addstate(nlist, &nn, p->out);
      }
    }
    {
      int *t = clist;
      clist = nlist;
      nlist = t;
      cn = nn;
    }
  }
  free(clist);
  free(nlist);
  free(mark);
  return last;
}

/* ---- 4. lazy DFA: each DFA state is a set of NFA states, built on first use, cached ---- */

typedef struct {
  int *set; /* sorted NFA state ids */
  int n;
  int accept;
  int next[256]; /* -1: not computed yet */
  int chain;     /* next entry in the same hash bucket */
} DState;

enum { NBUCKET = 4096 };

static DState *ds;
static int nds;
static int dcap;
static int bucket[NBUCKET];
static unsigned long long built;
static unsigned long long flushes;

static int cmpint(const void *a, const void *b) {
  int x = *(const int *)a;
  int y = *(const int *)b;
  return (x > y) - (x < y);
}

static unsigned hashset(const int *set, int n) {
  unsigned h = 2166136261u;
  int i;
  for (i = 0; i < n; i++) {
    h = (h ^ (unsigned)set[i]) * 16777619u;
  }
  return h % NBUCKET;
}

static void flush(void) {
  int i;
  for (i = 0; i < nds; i++) {
    free(ds[i].set);
  }
  nds = 0;
  for (i = 0; i < NBUCKET; i++) {
    bucket[i] = -1;
  }
  flushes++;
}

/* Return the cached state for SET (sorted, N ids), creating it, flushing the cache first if full. */
static int dstate(int *set, int n) {
  unsigned h = hashset(set, n);
  int k;
  int i;
  for (k = bucket[h]; k >= 0; k = ds[k].chain) {
    if (ds[k].n == n && memcmp(ds[k].set, set, (size_t)n * sizeof(int)) == 0) {
      return k;
    }
  }
  if (nds == dcap) {
    flush();
  }
  k = nds++;
  ds[k].set = xrealloc(NULL, (size_t)(n ? n : 1) * sizeof(int));
  memcpy(ds[k].set, set, (size_t)n * sizeof(int));
  ds[k].n = n;
  ds[k].accept = 0;
  for (i = 0; i < n; i++) {
    if (st[set[i]].op == MATCH) {
      ds[k].accept = 1;
    }
  }
  for (i = 0; i < 256; i++) {
    ds[k].next[i] = -1;
  }
  ds[k].chain = bucket[h];
  bucket[h] = k;
  built++;
  return k;
}

static long lazydfa(int start) {
  int *set = xrealloc(NULL, (size_t)nst * sizeof(int));
  int *src = xrealloc(NULL, (size_t)nst * sizeof(int));
  int n = 0;
  int srcn;
  int cur;
  int j;
  long last = -1;
  unsigned long long bytes = 0;
  size_t i;
  ds = xrealloc(NULL, (size_t)dcap * sizeof *ds);
  for (j = 0; j < NBUCKET; j++) {
    bucket[j] = -1;
  }
  mark = calloc((size_t)nst, sizeof(int));
  if (mark == NULL) {
    die("out of memory");
  }
  gen = 1;
  addstate(set, &n, start);
  qsort(set, (size_t)n, sizeof(int), cmpint);
  cur = dstate(set, n);
  for (i = 0;; i++) {
    int c;
    if (ds[cur].accept) {
      last = (long)i;
    }
    if (i == slen || ds[cur].n == 0) {
      break;
    }
    c = (unsigned char)subj[i];
    if (ds[cur].next[c] < 0) {
      /* Cache miss: step the NFA set, then find or build the target DFA state. */
      int target;
      srcn = ds[cur].n;
      memcpy(src, ds[cur].set, (size_t)srcn * sizeof(int));
      gen++;
      n = 0;
      for (j = 0; j < srcn; j++) {
        State *p = &st[src[j]];
        if ((p->op == CHAR && p->c == c) || p->op == ANY) {
          addstate(set, &n, p->out);
        }
      }
      qsort(set, (size_t)n, sizeof(int), cmpint);
      {
        unsigned long long before = flushes;
        target = dstate(set, n);
        if (flushes != before) {
          /* The flush freed the source state too: rebuild it so the edge can be recorded. */
          cur = dstate(src, srcn);
          target = dstate(set, n);
        }
      }
      ds[cur].next[c] = target;
    }
    cur = ds[cur].next[c];
    bytes++;
  }
  free(set);
  free(src);
  free(mark);
  steps = bytes; /* for the DFA, a step is one byte consumed */
  return last;
}

static void readsubject(void) {
  size_t cap = 4096;
  size_t n;
  char *buf = xrealloc(NULL, cap);
  slen = 0;
  while ((n = fread(buf + slen, 1, cap - slen, stdin)) > 0) {
    slen += n;
    if (slen == cap) {
      cap *= 2;
      buf = xrealloc(buf, cap);
    }
  }
  if (ferror(stdin)) {
    die("read error on stdin");
  }
  if (slen > 0 && buf[slen - 1] == '\n') {
    slen--;
  }
  subj = buf;
}

static unsigned long long number(const char *s, const char *what) {
  char *end;
  unsigned long long v = strtoull(s, &end, 10);
  if (*s == '\0' || *end != '\0') {
    fprintf(stderr, "engines: bad %s: %s\n", what, s);
    exit(2);
  }
  return v;
}

int main(int argc, char **argv) {
  const char *mode;
  int start;
  long end = -1;
  if (argc < 3) {
    die("usage: engines backtrack|memo|thompson|dfa PATTERN [LIMIT|CAP] < subject, or engines gen N");
  }
  mode = argv[1];
  if (strcmp(mode, "gen") == 0) {
    unsigned long long n = number(argv[2], "length");
    unsigned long long k;
    unsigned long long x = 1;
    for (k = 0; k < n; k++) {
      x = (x * 69069u + 1u) % 4294967296u;
      putchar((x >> 16) % 2 ? 'a' : 'b');
    }
    putchar('\n');
    return 0;
  }
  start = compile(argv[2]);
  readsubject();
  if (strcmp(mode, "backtrack") == 0) {
    limit = argc > 3 ? number(argv[3], "limit") : 0;
    end = bt(start, 0);
    if (aborted) {
      printf("engine=backtrack nfa=%d match=limit steps=%llu\n", nst, steps);
      return 3;
    }
  } else if (strcmp(mode, "memo") == 0) {
    size_t bits = (size_t)nst * (slen + 1);
    visited = calloc(bits / 8 + 1, 1);
    if (visited == NULL) {
      die("out of memory");
    }
    end = memo(start, 0);
  } else if (strcmp(mode, "thompson") == 0) {
    end = thompson(start);
  } else if (strcmp(mode, "dfa") == 0) {
    if (argc < 4) {
      die("dfa needs a cache size");
    }
    dcap = (int)number(argv[3], "cache size");
    if (dcap < 2) {
      die("cache size must be at least 2");
    }
    end = lazydfa(start);
    printf("engine=dfa nfa=%d match=%d end=%ld steps=%llu states=%llu flushes=%llu\n", nst,
           end >= 0, end, steps, built, flushes);
    return end >= 0 ? 0 : 1;
  } else {
    die("unknown mode");
  }
  printf("engine=%s nfa=%d match=%d end=%ld steps=%llu\n", mode, nst, end >= 0, end, steps);
  return end >= 0 ? 0 : 1;
}
