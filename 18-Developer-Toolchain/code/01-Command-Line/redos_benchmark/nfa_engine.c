/*
 * nfa_engine.c - Ken Thompson NFA Simulation vs Recursive Backtracking
 *
 * Implements:
 * 1. Thompson NFA simulation with linear time O(m * n) guarantee (Ken Thompson 1968, Russ Cox 2007).
 * 2. Recursive backtracking depth-first search demonstrating exponential O(2^n) catastrophic backtracking.
 * 3. Bounded Backtracker (BitState) memoizing (state, pos) pairs in a bitset to achieve linear time.
 * 4. PCRE2-style match limit guard triggering PCRE2_ERROR_MATCHLIMIT (-47) when operations exceed threshold.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    Match = 256,
    Split = 257
};

typedef struct State {
    int c;
    struct State *out;
    struct State *out1;
    int lastlist;
    int id;
} State;

static State matchstate = { Match, NULL, NULL, 0, 0 };
static int nstate = 1;
static int listid = 0;
static long long thompson_steps = 0;
static long long backtracker_steps = 0;
static long long bitstate_steps = 0;

static State* state(int c, State *out, State *out1) {
    State *s = (State*)malloc(sizeof(State));
    s->c = c;
    s->out = out;
    s->out1 = out1;
    s->lastlist = 0;
    s->id = nstate++;
    return s;
}

typedef struct Ptrlist {
    struct Ptrlist *next;
    State **s;
} Ptrlist;

typedef struct Frag {
    State *start;
    Ptrlist *out;
} Frag;

static Ptrlist* list1(State **outp) {
    Ptrlist *l = (Ptrlist*)malloc(sizeof(Ptrlist));
    l->next = NULL;
    l->s = outp;
    return l;
}

static void patch_list(Ptrlist *l, State *s) {
    while (l) {
        Ptrlist *next = l->next;
        *(l->s) = s;
        free(l);
        l = next;
    }
}

/* Compiles postfix regex: literals, '+', '.' */
static State* post2nfa(const char *postfix) {
    Frag stack[100], *sp = stack;
    for (; *postfix; postfix++) {
        switch (*postfix) {
        case '.': {
            Frag e2 = *--sp;
            Frag e1 = *--sp;
            patch_list(e1.out, e2.start);
            *sp++ = (Frag){ e1.start, e2.out };
            break;
        }
        case '+': {
            Frag e = *--sp;
            State *s = state(Split, e.start, NULL);
            patch_list(e.out, s);
            *sp++ = (Frag){ e.start, list1(&s->out1) };
            break;
        }
        default: {
            State *s = state(*postfix, NULL, NULL);
            *sp++ = (Frag){ s, list1(&s->out) };
            break;
        }
        }
    }
    Frag e = *--sp;
    patch_list(e.out, &matchstate);
    return e.start;
}

typedef struct List {
    State **s;
    int n;
} List;

static void addstate(List *l, State *s) {
    if (!s || s->lastlist == listid) return;
    s->lastlist = listid;
    thompson_steps++;
    if (s->c == Split) {
        addstate(l, s->out);
        addstate(l, s->out1);
        return;
    }
    l->s[l->n++] = s;
}

static int match_thompson(State *start, const char *text) {
    thompson_steps = 0;
    List l1, l2;
    l1.s = (State**)malloc((size_t)nstate * sizeof(State*));
    l2.s = (State**)malloc((size_t)nstate * sizeof(State*));
    l1.n = l2.n = 0;

    listid++;
    List *clist = &l1;
    List *nlist = &l2;
    addstate(clist, start);

    for (; *text; text++) {
        listid++;
        nlist->n = 0;
        for (int i = 0; i < clist->n; i++) {
            if (clist->s[i]->c == *text) {
                addstate(nlist, clist->s[i]->out);
            }
        }
        List *tmp = clist;
        clist = nlist;
        nlist = tmp;
        if (clist->n == 0) break;
    }
    int matched = 0;
    for (int i = 0; i < clist->n; i++) {
        if (clist->s[i]->c == Match) matched = 1;
    }
    free(l1.s);
    free(l2.s);
    return matched;
}

static int match_backtracker_rec(State *s, const char *text, int pos, int len, int match_limit, int *error_code) {
    backtracker_steps++;
    if (match_limit > 0 && backtracker_steps > match_limit) {
        *error_code = -47; /* PCRE2_ERROR_MATCHLIMIT */
        return 0;
    }
    if (!s) return 0;
    if (s->c == Match) return pos == len;
    if (s->c == Split) {
        if (match_backtracker_rec(s->out, text, pos, len, match_limit, error_code)) return 1;
        if (*error_code != 0) return 0;
        return match_backtracker_rec(s->out1, text, pos, len, match_limit, error_code);
    }
    if (pos < len && s->c == text[pos]) {
        return match_backtracker_rec(s->out, text, pos + 1, len, match_limit, error_code);
    }
    return 0;
}

static int match_backtracker(State *start, const char *text, int match_limit, int *error_code) {
    backtracker_steps = 0;
    *error_code = 0;
    return match_backtracker_rec(start, text, 0, (int)strlen(text), match_limit, error_code);
}

static int match_bitstate_rec(State *s, const char *text, int pos, int len, unsigned char *visited, int stride) {
    bitstate_steps++;
    if (!s) return 0;
    int idx = s->id * stride + pos;
    int byte_idx = idx / 8;
    int bit_mask = 1 << (idx % 8);
    if (visited[byte_idx] & bit_mask) return 0; /* Already visited (s->id, pos) */
    visited[byte_idx] |= (unsigned char)bit_mask;

    if (s->c == Match) return pos == len;
    if (s->c == Split) {
        if (match_bitstate_rec(s->out, text, pos, len, visited, stride)) return 1;
        return match_bitstate_rec(s->out1, text, pos, len, visited, stride);
    }
    if (pos < len && s->c == text[pos]) {
        return match_bitstate_rec(s->out, text, pos + 1, len, visited, stride);
    }
    return 0;
}

static int match_bitstate(State *start, const char *text) {
    bitstate_steps = 0;
    int len = (int)strlen(text);
    int stride = len + 1;
    int total_bits = nstate * stride;
    int total_bytes = (total_bits + 7) / 8;
    unsigned char *visited = (unsigned char*)calloc((size_t)total_bytes, 1);
    int matched = match_bitstate_rec(start, text, 0, len, visited, stride);
    free(visited);
    return matched;
}

static int failures = 0;

#define CHECK(cond, ...) \
    do { \
        if (!(cond)) { \
            fprintf(stderr, "CHECK FAILED: " __VA_ARGS__); \
            fprintf(stderr, "\n"); \
            failures++; \
        } \
    } while (0)

int main(void) {
    /* Regex: (a+)+  postfix: a + + */
    State *start = post2nfa("a++");

    /* Positive control: all three engines must accept a^N, so a rejection below means no match, not a broken matcher. */
    {
        int err = 0;
        CHECK(match_thompson(start, "aaaa") == 1, "thompson must accept aaaa");
        CHECK(match_backtracker(start, "aaaa", 0, &err) == 1 && err == 0, "backtracker must accept aaaa");
        CHECK(match_bitstate(start, "aaaa") == 1, "bitstate must accept aaaa");
    }

    printf("=== C Thompson NFA vs Recursive Backtracking Benchmark ===\n");
    printf("Grammar: (a+)+ (nested repetitions) against non-matching payload a^N + '!'\n");
    printf("\n");
    printf("%-4s | %-14s | %-15s | %-14s\n", "N", "Thompson Steps", "Backtrack Steps", "BitState Steps");
    printf("-----+----------------+-----------------+---------------\n");

    int ns[] = { 4, 6, 8, 10, 12 };
    for (int i = 0; i < 5; i++) {
        int n = ns[i];
        char text[64];
        memset(text, 'a', (size_t)n);
        text[n] = '!';
        text[n + 1] = '\0';

        int m_th = match_thompson(start, text);
        long long th = thompson_steps;

        int err = 0;
        int m_bt = match_backtracker(start, text, 0, &err);
        long long bt = backtracker_steps;

        int m_bs = match_bitstate(start, text);
        long long bs = bitstate_steps;

        CHECK(!m_th && !m_bt && !m_bs, "all engines must reject a^%d!", n);
        CHECK(th == 4LL * n + 1, "thompson steps %lld != 4N+1 at N=%d", th, n);
        CHECK(bt == 5LL * (1LL << n) - 4, "backtracker steps %lld != 5*2^N-4 at N=%d", bt, n);
        CHECK(bs == 5LL * n + 1, "bitstate steps %lld != 5N+1 at N=%d", bs, n);

        printf("%-4d | %-14lld | %-15lld | %-14lld\n", n, th, bt, bs);
    }

    printf("\n");
    printf("Resource limit enforcement check:\n");
    int err = 0;
    char text[64];
    memset(text, 'a', 20);
    text[20] = '!';
    text[21] = '\0';
    match_backtracker(start, text, 1000, &err);
    printf("Backtracker on N=20 with match_limit=1000: steps=%lld, status=%d (PCRE2_ERROR_MATCHLIMIT)\n",
           backtracker_steps, err);
    CHECK(err == -47 && backtracker_steps == 1001, "limit must abort at step 1001 with -47");

    /* Control: a limit above the work needed must not fire. */
    text[4] = '!';
    text[5] = '\0';
    match_backtracker(start, text, 1000, &err);
    printf("Backtracker on N=4 with match_limit=1000: steps=%lld, status=%d\n", backtracker_steps, err);
    CHECK(err == 0 && backtracker_steps == 76, "limit must not fire when steps stay under it");

    return failures ? 1 : 0;
}
