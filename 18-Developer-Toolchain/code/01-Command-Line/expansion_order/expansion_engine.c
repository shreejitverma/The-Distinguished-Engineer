#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <glob.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_WORDS 64
#define MAX_WORD_LEN 256

/*
 * Origin of every character in a word.
 * The shell must remember this per character, because later stages act only on
 * characters of a given origin.
 *
 * Q_LITERAL:   unquoted source text (can glob, can start a brace expression)
 * Q_SINGLE:    inside '...', including the two delimiters
 * Q_DOUBLE:    inside "...", including the two delimiters
 * Q_EXPAND:    produced by an unquoted parameter expansion (splits, globs, never unquoted)
 * Q_EXPAND_DQ: produced by a parameter expansion inside "..." (no split, no glob, kept)
 * Q_ESC:       an unquoted backslash (removed by quote removal)
 * Q_ESCCHR:    the character a backslash protected (never split, never globbed)
 */
typedef enum {
    Q_LITERAL = 0,
    Q_SINGLE,
    Q_DOUBLE,
    Q_EXPAND,
    Q_EXPAND_DQ,
    Q_ESC,
    Q_ESCCHR
} QuoteState;

typedef struct {
    char text[MAX_WORD_LEN];
    unsigned char qmask[MAX_WORD_LEN];
    size_t len;
} ShellWord;

typedef struct {
    ShellWord words[MAX_WORDS];
    size_t count;
} WordList;

static void die(const char *msg) {
    fprintf(stderr, "expansion_engine: %s\n", msg);
    exit(2);
}

static void sw_init(ShellWord *w) {
    w->len = 0;
    w->text[0] = '\0';
}

static void sw_push(ShellWord *w, char c, QuoteState q) {
    if (w->len + 1 >= MAX_WORD_LEN) {
        die("word too long");
    }
    w->text[w->len] = c;
    w->qmask[w->len] = (unsigned char)q;
    w->len++;
    w->text[w->len] = '\0';
}

static void wl_init(WordList *wl) {
    wl->count = 0;
}

static void wl_add(WordList *wl, const ShellWord *w) {
    if (wl->count >= MAX_WORDS) {
        die("too many words");
    }
    wl->words[wl->count++] = *w;
}

static void print_word_list(const char *stage_name, const WordList *wl) {
    printf("  %-28s [%zu word(s)]:\n", stage_name, wl->count);
    for (size_t i = 0; i < wl->count; i++) {
        printf("    [%zu] \"%s\"\n", i, wl->words[i].text);
    }
}

/* Fixed parameter table standing in for the shell's variables. */
static const char *lookup_env(const char *key) {
    if (strcmp(key, "TAG") == 0) return "v1.0 release";
    if (strcmp(key, "ARGS") == 0) return "--mode=fast -v";
    if (strcmp(key, "N") == 0) return "3";
    if (strcmp(key, "QUOTED") == 0) return "\"nested quote\"";
    if (strcmp(key, "PATTERN") == 0) return "*.tmp";
    if (strcmp(key, "PATHLIST") == 0) return ":a::b:";
    return "";
}

/* Copy src[from, to) into dst, keeping each character's origin. */
static void sw_copy_range(ShellWord *dst, const ShellWord *src, size_t from, size_t to) {
    for (size_t k = from; k < to; k++) {
        sw_push(dst, src->text[k], (QuoteState)src->qmask[k]);
    }
}

/*
 * Stage 1: Brace Expansion.
 * Purely textual and it runs before any parameter is evaluated.
 * Models the first unquoted {...} in a word: a comma list {A,B,C} (empty
 * alternatives kept) or an integer range {1..3}.
 * A range with a non-numeric end such as {1..$N} is left unchanged.
 * Limits: no nesting, no increment, no alternatives containing quotes.
 */
static void stage1_brace_expand(const WordList *in, WordList *out) {
    wl_init(out);
    for (size_t i = 0; i < in->count; i++) {
        const ShellWord *src = &in->words[i];
        size_t open_idx = 0, close_idx = 0;
        int have_open = 0, have_close = 0;

        for (size_t j = 0; j < src->len; j++) {
            if (src->qmask[j] != Q_LITERAL) continue;
            if (src->text[j] == '{' && !have_open) {
                have_open = 1;
                open_idx = j;
            } else if (src->text[j] == '}' && have_open) {
                have_close = 1;
                close_idx = j;
                break;
            }
        }

        if (!(have_open && have_close && close_idx > open_idx + 1)) {
            wl_add(out, src);
            continue;
        }

        char inner[MAX_WORD_LEN];
        size_t inner_len = close_idx - open_idx - 1;
        memcpy(inner, src->text + open_idx + 1, inner_len);
        inner[inner_len] = '\0';

        char *dots = strstr(inner, "..");
        if (dots) {
            *dots = '\0';
            const char *start_str = inner;
            const char *end_str = dots + 2;
            char *p1, *p2;
            long start_val = strtol(start_str, &p1, 10);
            long end_val = strtol(end_str, &p2, 10);
            if (*start_str != '\0' && *end_str != '\0' && *p1 == '\0' && *p2 == '\0') {
                long step = (start_val <= end_val) ? 1 : -1;
                for (long val = start_val;; val += step) {
                    ShellWord nw;
                    sw_init(&nw);
                    sw_copy_range(&nw, src, 0, open_idx);
                    char numbuf[32];
                    snprintf(numbuf, sizeof(numbuf), "%ld", val);
                    for (size_t k = 0; numbuf[k]; k++) sw_push(&nw, numbuf[k], Q_LITERAL);
                    sw_copy_range(&nw, src, close_idx + 1, src->len);
                    wl_add(out, &nw);
                    if (val == end_val) break;
                }
            } else {
                wl_add(out, src);
            }
            continue;
        }

        if (strchr(inner, ',') == NULL) {
            wl_add(out, src);
            continue;
        }

        const char *alt = inner;
        for (;;) {
            const char *comma = strchr(alt, ',');
            size_t alt_len = comma ? (size_t)(comma - alt) : strlen(alt);
            ShellWord nw;
            sw_init(&nw);
            sw_copy_range(&nw, src, 0, open_idx);
            for (size_t k = 0; k < alt_len; k++) sw_push(&nw, alt[k], Q_LITERAL);
            sw_copy_range(&nw, src, close_idx + 1, src->len);
            wl_add(out, &nw);
            if (!comma) break;
            alt = comma + 1;
        }
    }
}

/*
 * Stage 2: Tilde and Parameter Expansion in one left-to-right scan.
 * A leading unquoted ~ becomes HOME.
 * $NAME is replaced by its value, except under single quotes or after a backslash.
 * Inserted text is tagged Q_EXPAND when unquoted and Q_EXPAND_DQ inside double
 * quotes; it never gets the original syntactic tags, so a quote character in a
 * value stays plain data.
 */
static void stage2_param_and_tilde_expand(const WordList *in, WordList *out, const char *home) {
    wl_init(out);
    for (size_t i = 0; i < in->count; i++) {
        const ShellWord *src = &in->words[i];
        ShellWord nw;
        sw_init(&nw);
        size_t j = 0;

        if (src->len > 0 && src->text[0] == '~' && src->qmask[0] == Q_LITERAL &&
            (src->len == 1 || src->text[1] == '/')) {
            for (size_t h = 0; home[h]; h++) sw_push(&nw, home[h], Q_LITERAL);
            j = 1;
        }

        while (j < src->len) {
            QuoteState origin = (QuoteState)src->qmask[j];
            int can_expand = (origin == Q_LITERAL || origin == Q_DOUBLE);
            if (src->text[j] == '$' && can_expand) {
                size_t k = j + 1;
                char varname[64];
                size_t vlen = 0;
                while (k < src->len && vlen < sizeof(varname) - 1 &&
                       (isalnum((unsigned char)src->text[k]) || src->text[k] == '_')) {
                    varname[vlen++] = src->text[k++];
                }
                varname[vlen] = '\0';
                if (vlen > 0) {
                    const char *val = lookup_env(varname);
                    QuoteState tag = (origin == Q_DOUBLE) ? Q_EXPAND_DQ : Q_EXPAND;
                    for (size_t v = 0; val[v]; v++) sw_push(&nw, val[v], tag);
                    j = k;
                    continue;
                }
            }
            sw_push(&nw, src->text[j], origin);
            j++;
        }
        wl_add(out, &nw);
    }
}

static int is_ifs_char(const char *ifs, char c) {
    return c != '\0' && strchr(ifs, c) != NULL;
}

static int is_ifs_space(char c) {
    return c == ' ' || c == '\t' || c == '\n';
}

/*
 * Stage 3: Field Splitting (POSIX.1-2024 2.6.5).
 * Only Q_EXPAND characters can be delimiters.
 * IFS white space (space, tab, newline found in IFS) collapses and is trimmed.
 * Every other IFS character is a delimiter of its own, so adjacent ones make
 * an empty field, but a trailing one makes no trailing empty field.
 * White space next to a non-white-space delimiter belongs to that delimiter.
 * An empty IFS disables splitting.
 */
static void stage3_field_splitting(const WordList *in, WordList *out, const char *ifs) {
    wl_init(out);
    for (size_t i = 0; i < in->count; i++) {
        const ShellWord *src = &in->words[i];
        ShellWord cur;
        sw_init(&cur);
        int have_field = 0; /* cur holds source text or expansion output */
        int closed_by_ws = 0; /* a field was just closed by IFS white space */

        for (size_t j = 0; j < src->len; j++) {
            char c = src->text[j];
            if (src->qmask[j] == Q_EXPAND && is_ifs_char(ifs, c)) {
                if (is_ifs_space(c)) {
                    if (have_field) {
                        wl_add(out, &cur);
                        sw_init(&cur);
                        have_field = 0;
                        closed_by_ws = 1;
                    }
                } else if (have_field) {
                    wl_add(out, &cur);
                    sw_init(&cur);
                    have_field = 0;
                    closed_by_ws = 0;
                } else if (closed_by_ws) {
                    closed_by_ws = 0; /* same delimiter as the white space before it */
                } else {
                    sw_init(&cur); /* adjacent or leading delimiter: empty field */
                    wl_add(out, &cur);
                }
            } else {
                sw_push(&cur, c, (QuoteState)src->qmask[j]);
                have_field = 1;
                closed_by_ws = 0;
            }
        }
        if (have_field) {
            wl_add(out, &cur);
        }
    }
}

static int is_glob_char(char c) {
    return c == '*' || c == '?' || c == '[';
}

/*
 * Stage 4: Pathname Expansion with glob(3) against the real current directory.
 * Only unquoted characters (Q_LITERAL, Q_EXPAND) act as pattern characters.
 * Every protected character is backslash-escaped in the pattern handed to
 * glob(3), and syntactic quote delimiters are dropped from it.
 * No match leaves the word unchanged, as POSIX requires.
 */
static void stage4_pathname_expansion(const WordList *in, WordList *out) {
    wl_init(out);
    for (size_t i = 0; i < in->count; i++) {
        const ShellWord *src = &in->words[i];
        char pattern[2 * MAX_WORD_LEN];
        size_t plen = 0;
        int has_glob = 0;

        for (size_t j = 0; j < src->len; j++) {
            char c = src->text[j];
            QuoteState q = (QuoteState)src->qmask[j];
            int unquoted = (q == Q_LITERAL || q == Q_EXPAND);
            if ((q == Q_SINGLE && c == '\'') || (q == Q_DOUBLE && c == '"') || q == Q_ESC) {
                continue;
            }
            if (unquoted && is_glob_char(c)) {
                has_glob = 1;
            }
            if (!unquoted && (is_glob_char(c) || c == '\\')) {
                pattern[plen++] = '\\';
            }
            pattern[plen++] = c;
        }
        pattern[plen] = '\0';

        if (has_glob) {
            glob_t g;
            if (glob(pattern, 0, NULL, &g) == 0) {
                for (size_t k = 0; k < g.gl_pathc; k++) {
                    ShellWord nw;
                    sw_init(&nw);
                    for (size_t l = 0; g.gl_pathv[k][l]; l++) {
                        sw_push(&nw, g.gl_pathv[k][l], Q_LITERAL);
                    }
                    wl_add(out, &nw);
                }
                globfree(&g);
                continue;
            }
            globfree(&g);
        }
        wl_add(out, src);
    }
}

/*
 * Stage 5: Quote Removal.
 * Deletes the quote delimiters and the backslashes that the lexer tagged.
 * Quote characters that came from a parameter expansion carry Q_EXPAND or
 * Q_EXPAND_DQ, so they are never touched.
 */
static void stage5_quote_removal(const WordList *in, WordList *out) {
    wl_init(out);
    for (size_t i = 0; i < in->count; i++) {
        const ShellWord *src = &in->words[i];
        ShellWord nw;
        sw_init(&nw);
        for (size_t j = 0; j < src->len; j++) {
            char c = src->text[j];
            QuoteState q = (QuoteState)src->qmask[j];
            if ((q == Q_SINGLE && c == '\'') || (q == Q_DOUBLE && c == '"') || q == Q_ESC) {
                continue;
            }
            sw_push(&nw, c, q);
        }
        wl_add(out, &nw);
    }
}

/*
 * Stage 0: tokenization of a command line into words.
 * Unquoted blanks delimit words; '...' and "..." are tracked per character;
 * an unquoted backslash protects the next character.
 * Backslashes inside double quotes and operators are not modeled.
 */
static void stage0_lex(const char *line, WordList *out) {
    wl_init(out);
    ShellWord cur;
    sw_init(&cur);
    int in_word = 0;
    QuoteState state = Q_LITERAL;

    for (size_t i = 0; line[i]; i++) {
        char c = line[i];
        if (state == Q_SINGLE) {
            sw_push(&cur, c, Q_SINGLE);
            if (c == '\'') state = Q_LITERAL;
        } else if (state == Q_DOUBLE) {
            sw_push(&cur, c, Q_DOUBLE);
            if (c == '"') state = Q_LITERAL;
        } else if (c == ' ' || c == '\t') {
            if (in_word) {
                wl_add(out, &cur);
                sw_init(&cur);
                in_word = 0;
            }
        } else if (c == '\'') {
            sw_push(&cur, c, Q_SINGLE);
            state = Q_SINGLE;
            in_word = 1;
        } else if (c == '"') {
            sw_push(&cur, c, Q_DOUBLE);
            state = Q_DOUBLE;
            in_word = 1;
        } else if (c == '\\' && line[i + 1] != '\0') {
            sw_push(&cur, c, Q_ESC);
            sw_push(&cur, line[i + 1], Q_ESCCHR);
            i++;
            in_word = 1;
        } else {
            sw_push(&cur, c, Q_LITERAL);
            in_word = 1;
        }
    }
    if (state != Q_LITERAL) die("unterminated quote");
    if (in_word) wl_add(out, &cur);
}

static void run_trace(const char *label, const char *raw_input, const char *ifs) {
    static WordList w[6]; /* ~100 KB each, so keep them off the stack */

    printf("============================================================\n");
    printf("TRACE: %s\n", label);
    printf("Raw Input: %s\n", raw_input);

    stage0_lex(raw_input, &w[0]);
    print_word_list("Stage 0: Lexed Words", &w[0]);

    stage1_brace_expand(&w[0], &w[1]);
    print_word_list("Stage 1: Brace Expansion", &w[1]);

    stage2_param_and_tilde_expand(&w[1], &w[2], "/home/user");
    print_word_list("Stage 2: Parameter & Tilde", &w[2]);

    stage3_field_splitting(&w[2], &w[3], ifs);
    print_word_list("Stage 3: Field Splitting (IFS)", &w[3]);

    stage4_pathname_expansion(&w[3], &w[4]);
    print_word_list("Stage 4: Pathname Expansion", &w[4]);

    stage5_quote_removal(&w[4], &w[5]);
    print_word_list("Stage 5: Quote Removal (Final)", &w[5]);
}

int main(void) {
    printf("POSIX/BASH 5-STAGE WORD EXPANSION ENGINE\n");
    printf("Deterministic Reference Trace (Standard C99)\n");

    run_trace("Brace expansion with quoted parameter", "pkg_{core,net}_\"$TAG\"", " \t\n");
    run_trace("Unquoted parameter with whitespace splitting", "cmd $ARGS", " \t\n");
    run_trace("Range brace containing variable {1..$N}", "{1..$N}", " \t\n");
    run_trace("Variable value containing quotes ($QUOTED)", "call $QUOTED", " \t\n");
    run_trace("Glob on an expanded pattern, quoted and unquoted", "ls data_$PATTERN \"data_$PATTERN\"", " \t\n");
    run_trace("Non-whitespace IFS (IFS=:)", "list $PATHLIST", ":");
    run_trace("Backslash and single quotes protect characters", "echo a\\ b \\*.tmp \\$TAG '$TAG'", " \t\n");
    run_trace("Numeric range and an empty brace alternative", "echo n{1..3} {x,}y", " \t\n");

    return 0;
}
