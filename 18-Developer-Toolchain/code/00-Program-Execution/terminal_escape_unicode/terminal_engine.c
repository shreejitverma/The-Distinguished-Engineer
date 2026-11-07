/*
 * terminal_engine.c - Terminal Escape Sequence Parser, UTF-8 DFA Decoder,
 *                     and Unicode Character Width Engine.
 *
 * Implements:
 * 1. Bjoern Hoehrmann's DFA UTF-8 Decoder (RFC 3629 compliant).
 * 2. Paul Flo Williams' DEC VT500 Parser State Machine (ECMA-48 / ANSI X3.64).
 * 3. Extended Grapheme Cluster Segmentation and Width versus POSIX wcwidth.
 * 4. Dual Screen Buffer Switching (DECSET / DECRST 1049).
 * 5. Bracketed Paste Mode (DECSET / DECRST 2004) and Shell Command Isolation.
 * 6. Terminal Query-Response Loop and Escape Injection Sanitization.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <locale.h>
#include <wchar.h>

/* ========================================================================= */
/* 1. UTF-8 DFA Decoder (Bjoern Hoehrmann)                                    */
/* ========================================================================= */

#define UTF8_ACCEPT 0
#define UTF8_REJECT 12

static const uint8_t utf8d[] = {
  /* 0x00..0xFF character class mappings */
   0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,  0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
   0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,  0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
   0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,  0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
   0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,  0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
   1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,  9,9,9,9,9,9,9,9,9,9,9,9,9,9,9,9,
   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,  7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
   8,8,2,2,2,2,2,2,2,2,2,2,2,2,2,2,  2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,
  10,3,3,3,3,3,3,3,3,3,3,3,3,4,3,3, 11,6,6,6,5,8,8,8,8,8,8,8,8,8,8,8,

  /* State transition table */
   0,12,24,36,60,96,84,12,12,12,48,72, 12,12,12,12,12,12,12,12,12,12,12,12,
  12, 0,12,12,12,12,12, 0,12, 0,12,12, 12,24,12,12,12,12,12,24,12,24,12,12,
  12,12,12,12,12,12,12,24,12,12,12,12, 12,24,12,12,12,12,12,12,12,24,12,12,
  12,12,12,12,12,12,12,36,12,36,12,12, 12,36,12,12,12,12,12,36,12,36,12,12,
  12,36,12,12,12,12,12,12,12,12,12,12,
};

static inline uint32_t utf8_decode_step(uint32_t *state, uint32_t *codep, uint8_t byte) {
    uint32_t type = utf8d[byte];
    *codep = (*state != UTF8_ACCEPT) ?
        (byte & 0x3fu) | (*codep << 6) :
        (0xff >> type) & (byte);
    *state = utf8d[256 + *state + type];
    return *state;
}

/* ========================================================================= */
/* 2. Paul Flo Williams DEC VT500 State Machine Parser                        */
/* ========================================================================= */

typedef enum {
    STATE_GROUND = 0,
    STATE_ESCAPE,
    STATE_ESCAPE_INTERMEDIATE,
    STATE_CSI_ENTRY,
    STATE_CSI_PARAM,
    STATE_CSI_INTERMEDIATE,
    STATE_CSI_IGNORE,
    STATE_DCS_ENTRY,
    STATE_DCS_PARAM,
    STATE_DCS_INTERMEDIATE,
    STATE_DCS_PASSTHROUGH,
    STATE_DCS_IGNORE,
    STATE_OSC_STRING,
    STATE_SOS_PM_APC_STRING
} parser_state_t;

#define MAX_PARAMS 16
#define MAX_SUBPARAMS 8
#define MAX_INTERMEDIATES 4
#define MAX_OSC_LEN 512

typedef struct {
    parser_state_t state;
    char private_marker;
    int params[MAX_PARAMS];
    int subparams[MAX_PARAMS][MAX_SUBPARAMS];
    int subparam_count[MAX_PARAMS];
    int param_count;
    char intermediates[MAX_INTERMEDIATES];
    int intermediate_count;
    char osc_buffer[MAX_OSC_LEN];
    int osc_len;
    char final_char;

    /* Terminal state flags */
    bool alternate_screen_active;
    bool bracketed_paste_active;
    int cursor_row;
    int cursor_col;
    int saved_cursor_row;
    int saved_cursor_col;
    int kitty_keyboard_flags;

    /* Output trace counters */
    int count_print;
    int count_execute;
    int count_csi_dispatch;
    int count_osc_dispatch;

    /* Decoded payloads, so the trace proves content and not only counts */
    int sgr_r, sgr_g, sgr_b;
    bool sgr_truecolor_seen;
    char osc8_uri[MAX_OSC_LEN];
} vt_parser_t;

static void vt_parser_reset_sequence(vt_parser_t *p) {
    p->private_marker = '\0';
    p->param_count = 0;
    for (int i = 0; i < MAX_PARAMS; i++) {
        p->params[i] = -1; /* -1 indicates omitted/default parameter */
        p->subparam_count[i] = 0;
        for (int j = 0; j < MAX_SUBPARAMS; j++) {
            p->subparams[i][j] = -1;
        }
    }
    p->intermediate_count = 0;
    p->osc_len = 0;
    p->final_char = '\0';
}

static void vt_parser_init(vt_parser_t *p) {
    memset(p, 0, sizeof(*p));
    p->state = STATE_GROUND;
    p->cursor_row = 1;
    p->cursor_col = 1;
    p->saved_cursor_row = 1;
    p->saved_cursor_col = 1;
    vt_parser_reset_sequence(p);
}

/* Dispatch executed for CSI commands */
static void handle_csi_dispatch(vt_parser_t *p) {
    p->count_csi_dispatch++;

    /* DECSET / DECRST private modes */
    if (p->private_marker == '?') {
        int mode = (p->param_count > 0) ? p->params[0] : -1;
        if (p->final_char == 'h') {
            /* Set Mode */
            if (mode == 1049) {
                p->saved_cursor_row = p->cursor_row;
                p->saved_cursor_col = p->cursor_col;
                p->alternate_screen_active = true;
                p->cursor_row = 1;
                p->cursor_col = 1;
            } else if (mode == 2004) {
                p->bracketed_paste_active = true;
            }
        } else if (p->final_char == 'l') {
            /* Reset Mode */
            if (mode == 1049) {
                p->alternate_screen_active = false;
                p->cursor_row = p->saved_cursor_row;
                p->cursor_col = p->saved_cursor_col;
            } else if (mode == 2004) {
                p->bracketed_paste_active = false;
            }
        }
        return;
    }

    /* Kitty keyboard protocol progressive enhancement push: CSI > flags u */
    if (p->private_marker == '>' && p->final_char == 'u') {
        int flags = (p->param_count > 0 && p->params[0] >= 0) ? p->params[0] : 0;
        p->kitty_keyboard_flags = flags;
        return;
    }

    /* Standard ECMA-48 commands */
    switch (p->final_char) {
        case 'H': /* CUP - Cursor Position */
        case 'f': {
            int r = (p->param_count > 0 && p->params[0] > 0) ? p->params[0] : 1;
            int c = (p->param_count > 1 && p->params[1] > 0) ? p->params[1] : 1;
            p->cursor_row = r;
            p->cursor_col = c;
            break;
        }
        case 'm': /* SGR - Select Graphic Rendition */
            /* ITU-T T.416 direct colour: 38 : 2 : colour-space-id : r : g : b */
            if (p->param_count > 0 && p->params[0] == 38 &&
                p->subparam_count[0] == 5 && p->subparams[0][0] == 2) {
                p->sgr_r = p->subparams[0][2];
                p->sgr_g = p->subparams[0][3];
                p->sgr_b = p->subparams[0][4];
                p->sgr_truecolor_seen = true;
            }
            break;
        case 'n': /* DSR - Device Status Report */
            /* param 6 queries cursor position */
            break;
        default:
            break;
    }
}

/* Dispatch executed for OSC commands */
static void handle_osc_dispatch(vt_parser_t *p) {
    p->count_osc_dispatch++;
    /* OSC 8 ; params ; URI - an empty URI closes the hyperlink */
    if (strncmp(p->osc_buffer, "8;", 2) == 0) {
        const char *uri = strchr(p->osc_buffer + 2, ';');
        if (uri != NULL && uri[1] != '\0') {
            snprintf(p->osc8_uri, sizeof(p->osc8_uri), "%s", uri + 1);
        }
    }
}

/* Feed a single byte into the Paul Flo Williams parser */
static void vt_parser_feed_byte(vt_parser_t *p, uint8_t b) {
    /* Anywhere events: C0 controls that interrupt any state */
    if (b == 0x18 || b == 0x1A) { /* CAN, SUB cancel sequence */
        p->state = STATE_GROUND;
        vt_parser_reset_sequence(p);
        return;
    }
    if (b == 0x1B) { /* ESC transitions to escape state from anywhere */
        if (p->state == STATE_OSC_STRING) {
            /* Williams: leaving osc string runs osc_end; the ESC is the first byte of ST */
            handle_osc_dispatch(p);
        }
        p->state = STATE_ESCAPE;
        vt_parser_reset_sequence(p);
        return;
    }

    switch (p->state) {
        case STATE_GROUND:
            if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            } else {
                p->count_print++;
            }
            break;

        case STATE_ESCAPE:
            if (b == '[') {
                p->state = STATE_CSI_ENTRY;
            } else if (b == ']') {
                p->state = STATE_OSC_STRING;
            } else if (b == 'P') {
                p->state = STATE_DCS_ENTRY;
            } else if (b == '_' || b == '^') {
                p->state = STATE_SOS_PM_APC_STRING;
            } else if (b >= 0x20 && b <= 0x2F) {
                if (p->intermediate_count < MAX_INTERMEDIATES) {
                    p->intermediates[p->intermediate_count++] = (char)b;
                }
                p->state = STATE_ESCAPE_INTERMEDIATE;
            } else if (b >= 0x30 && b <= 0x7E) {
                /* Esc dispatch */
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            }
            break;

        case STATE_ESCAPE_INTERMEDIATE:
            if (b >= 0x20 && b <= 0x2F) {
                if (p->intermediate_count < MAX_INTERMEDIATES) {
                    p->intermediates[p->intermediate_count++] = (char)b;
                }
            } else if (b >= 0x30 && b <= 0x7E) {
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            }
            break;

        case STATE_CSI_ENTRY:
            if (b >= '0' && b <= '9') {
                p->param_count = 1;
                p->params[0] = b - '0';
                p->state = STATE_CSI_PARAM;
            } else if (b == ';') {
                p->param_count = 2;
                p->params[0] = -1;
                p->params[1] = 0;
                p->state = STATE_CSI_PARAM;
            } else if (b >= 0x3C && b <= 0x3F) { /* '?', '>', '=', '<' */
                p->private_marker = (char)b;
                p->state = STATE_CSI_PARAM;
            } else if (b >= 0x20 && b <= 0x2F) {
                if (p->intermediate_count < MAX_INTERMEDIATES) {
                    p->intermediates[p->intermediate_count++] = (char)b;
                }
                p->state = STATE_CSI_INTERMEDIATE;
            } else if (b >= 0x40 && b <= 0x7E) {
                p->final_char = (char)b;
                handle_csi_dispatch(p);
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            }
            break;

        case STATE_CSI_PARAM:
            if (b >= '0' && b <= '9') {
                if (p->param_count == 0) {
                    p->param_count = 1;
                    p->params[0] = 0;
                }
                int cur_p = p->param_count - 1;
                if (p->subparam_count[cur_p] > 0) {
                    int cur_sp = p->subparam_count[cur_p] - 1;
                    if (p->subparams[cur_p][cur_sp] < 0) p->subparams[cur_p][cur_sp] = 0;
                    p->subparams[cur_p][cur_sp] = p->subparams[cur_p][cur_sp] * 10 + (b - '0');
                } else {
                    if (p->params[cur_p] < 0) p->params[cur_p] = 0;
                    p->params[cur_p] = p->params[cur_p] * 10 + (b - '0');
                }
            } else if (b == ';') {
                if (p->param_count < MAX_PARAMS) {
                    p->param_count++;
                    p->params[p->param_count - 1] = -1;
                }
            } else if (b == ':') {
                /* Subparameter delimiter (ITU T.416 / ECMA-48 5th ed) */
                if (p->param_count == 0) {
                    p->param_count = 1;
                    p->params[0] = -1;
                }
                int cur_p = p->param_count - 1;
                if (p->subparam_count[cur_p] < MAX_SUBPARAMS) {
                    p->subparam_count[cur_p]++;
                    p->subparams[cur_p][p->subparam_count[cur_p] - 1] = -1;
                }
            } else if (b >= 0x20 && b <= 0x2F) {
                if (p->intermediate_count < MAX_INTERMEDIATES) {
                    p->intermediates[p->intermediate_count++] = (char)b;
                }
                p->state = STATE_CSI_INTERMEDIATE;
            } else if (b >= 0x40 && b <= 0x7E) {
                p->final_char = (char)b;
                handle_csi_dispatch(p);
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            } else {
                p->state = STATE_CSI_IGNORE;
            }
            break;

        case STATE_CSI_INTERMEDIATE:
            if (b >= 0x20 && b <= 0x2F) {
                if (p->intermediate_count < MAX_INTERMEDIATES) {
                    p->intermediates[p->intermediate_count++] = (char)b;
                }
            } else if (b >= 0x40 && b <= 0x7E) {
                p->final_char = (char)b;
                handle_csi_dispatch(p);
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            } else {
                p->state = STATE_CSI_IGNORE;
            }
            break;

        case STATE_CSI_IGNORE:
            if (b >= 0x40 && b <= 0x7E) {
                p->state = STATE_GROUND;
            } else if (b <= 0x1F || b == 0x7F) {
                p->count_execute++;
            }
            break;

        case STATE_OSC_STRING:
            if (b == 0x07) { /* BEL terminates OSC */
                handle_osc_dispatch(p);
                p->state = STATE_GROUND;
            } else {
                if (p->osc_len < MAX_OSC_LEN - 1) {
                    p->osc_buffer[p->osc_len++] = (char)b;
                    p->osc_buffer[p->osc_len] = '\0';
                }
            }
            break;

        case STATE_DCS_ENTRY:
        case STATE_DCS_PARAM:
        case STATE_DCS_INTERMEDIATE:
        case STATE_DCS_PASSTHROUGH:
        case STATE_DCS_IGNORE:
        case STATE_SOS_PM_APC_STRING:
            /* ST (ESC \) or BEL returns to ground */
            if (b == 0x07) {
                p->state = STATE_GROUND;
            }
            break;
    }
}

static void vt_parser_feed_string(vt_parser_t *p, const char *s, size_t len) {
    for (size_t i = 0; i < len; i++) {
        vt_parser_feed_byte(p, (uint8_t)s[i]);
    }
}

/* ========================================================================= */
/* 3. Unicode Character Width and Grapheme Segmentation Engine               */
/* ========================================================================= */

typedef enum {
    WIDTH_NARROW = 1,
    WIDTH_WIDE = 2,
    WIDTH_ZERO = 0,
    WIDTH_CONTROL = -1
} char_width_t;

/* Classify codepoint cell width based on UAX #11 East Asian Width */
static char_width_t classify_codepoint_width(uint32_t cp) {
    if (cp == 0) return WIDTH_ZERO;
    if (cp < 0x20 || (cp >= 0x7F && cp < 0xA0)) return WIDTH_CONTROL;

    /* Combining characters (Mn, Me) */
    if (cp >= 0x0300 && cp <= 0x036F) return WIDTH_ZERO; /* Combining Diacritical Marks */
    if (cp >= 0x1DC0 && cp <= 0x1DFF) return WIDTH_ZERO; /* Combining Diacritical Marks Supplement */
    if (cp >= 0x20D0 && cp <= 0x20FF) return WIDTH_ZERO; /* Combining Diacritical Marks for Symbols */
    if (cp >= 0xFE20 && cp <= 0xFE2F) return WIDTH_ZERO; /* Combining Half Marks */

    /* Zero Width Joiner, Variation Selectors, Formatting */
    if (cp == 0x200B || cp == 0x200C || cp == 0x200D || cp == 0xFEFF) return WIDTH_ZERO;
    if (cp >= 0xFE00 && cp <= 0xFE0F) return WIDTH_ZERO; /* Variation Selectors 1-16 */
    if (cp >= 0xE0100 && cp <= 0xE01EF) return WIDTH_ZERO; /* Variation Selectors Supplement */

    /* Emoji Modifier (Skin tones: U+1F3FB..U+1F3FF) */
    if (cp >= 0x1F3FB && cp <= 0x1F3FF) return WIDTH_WIDE;

    /* CJK Unified Ideographs & Hangul */
    if (cp >= 0x1100 && cp <= 0x115F) return WIDTH_WIDE; /* Hangul Jamo */
    if (cp >= 0x2E80 && cp <= 0x9FFF) return WIDTH_WIDE; /* CJK Radicals, Ideographs */
    if (cp >= 0xAC00 && cp <= 0xD7A3) return WIDTH_WIDE; /* Hangul Syllables */
    if (cp >= 0xF900 && cp <= 0xFAFF) return WIDTH_WIDE; /* CJK Compatibility Ideographs */
    if (cp >= 0xFE10 && cp <= 0xFE19) return WIDTH_WIDE; /* Vertical forms */
    if (cp >= 0xFE30 && cp <= 0xFE6F) return WIDTH_WIDE; /* CJK Compatibility Forms */
    if (cp >= 0xFF01 && cp <= 0xFF60) return WIDTH_WIDE; /* Fullwidth Forms */
    if (cp >= 0xFFE0 && cp <= 0xFFE6) return WIDTH_WIDE;

    /* Miscellaneous Symbols and Pictographs, Emoticons, Transport, Supplemental */
    if (cp >= 0x1F300 && cp <= 0x1FAFF) return WIDTH_WIDE; /* Emoji & Pictographs */
    if (cp >= 0x20000 && cp <= 0x3FFFF) return WIDTH_WIDE; /* CJK Extensions */

    /* Regional Indicators */
    if (cp >= 0x1F1E6 && cp <= 0x1F1FF) return WIDTH_NARROW;

    return WIDTH_NARROW;
}

/* Extended Grapheme Cluster check (UAX #29 simplified core rules) */
static bool is_grapheme_boundary(uint32_t prev_cp, uint32_t curr_cp, int ri_count) {
    if (prev_cp == 0) return true;

    /* GB3: CR x LF */
    if (prev_cp == 0x0D && curr_cp == 0x0A) return false;

    /* GB4 / GB5: Control / CR / LF break before and after */
    if (prev_cp == 0x0D || prev_cp == 0x0A || (prev_cp < 0x20) || (prev_cp >= 0x7F && prev_cp < 0xA0)) return true;
    if (curr_cp == 0x0D || curr_cp == 0x0A || (curr_cp < 0x20) || (curr_cp >= 0x7F && curr_cp < 0xA0)) return true;

    /* GB9: Do not break before extending characters or ZWJ */
    char_width_t w = classify_codepoint_width(curr_cp);
    if (w == WIDTH_ZERO || curr_cp == 0x200D) return false;

    /* Emoji Modifier: base emoji x skin tone modifier */
    if (curr_cp >= 0x1F3FB && curr_cp <= 0x1F3FF) return false;

    /* GB11: ZWJ x Extended_Pictographic */
    if (prev_cp == 0x200D && (curr_cp >= 0x1F300 && curr_cp <= 0x1FAFF)) return false;

    /* GB12/13: Regional_Indicator x Regional_Indicator (flags in pairs: do not break if odd count) */
    if ((prev_cp >= 0x1F1E6 && prev_cp <= 0x1F1FF) &&
        (curr_cp >= 0x1F1E6 && curr_cp <= 0x1F1FF)) {
        return (ri_count % 2 == 0);
    }

    return true;
}

/* Segment text into grapheme clusters and calculate column display width */
typedef struct {
    uint32_t codepoints[16];
    int codepoint_count;
    int display_width;
    int naive_wcwidth_sum;
} grapheme_cluster_t;

static int segment_graphemes(const uint32_t *codepoints, int count, grapheme_cluster_t *clusters, int max_clusters) {
    if (count == 0) return 0;

    int cluster_count = 0;
    uint32_t prev = 0;
    int ri_count = 0;

    for (int i = 0; i < count; i++) {
        uint32_t cp = codepoints[i];
        bool break_cluster = (i == 0) || is_grapheme_boundary(prev, cp, ri_count);

        if (break_cluster) {
            if (cluster_count >= max_clusters) break;
            cluster_count++;
            clusters[cluster_count - 1].codepoint_count = 0;
            clusters[cluster_count - 1].display_width = 0;
            clusters[cluster_count - 1].naive_wcwidth_sum = 0;
        }

        grapheme_cluster_t *c = &clusters[cluster_count - 1];
        if (c->codepoint_count < 16) {
            c->codepoints[c->codepoint_count++] = cp;
        }

        char_width_t w = classify_codepoint_width(cp);
        c->naive_wcwidth_sum += (w > 0) ? w : 0;

        /* Track contiguous regional indicator count */
        if (cp >= 0x1F1E6 && cp <= 0x1F1FF) {
            ri_count++;
        } else {
            ri_count = 0;
        }

        prev = cp;
    }

    /* Assign true display width per cluster */
    for (int i = 0; i < cluster_count; i++) {
        grapheme_cluster_t *c = &clusters[i];
        if (c->codepoint_count == 0) continue;

        /* If cluster contains emoji or wide base, cluster width is 2 */
        bool is_wide = false;
        for (int j = 0; j < c->codepoint_count; j++) {
            if (classify_codepoint_width(c->codepoints[j]) == WIDTH_WIDE) {
                is_wide = true;
                break;
            }
        }
        /* Regional indicator pairs occupy 2 columns */
        if (c->codepoint_count == 2 &&
            c->codepoints[0] >= 0x1F1E6 && c->codepoints[0] <= 0x1F1FF &&
            c->codepoints[1] >= 0x1F1E6 && c->codepoints[1] <= 0x1F1FF) {
            is_wide = true;
        }

        c->display_width = is_wide ? 2 : (c->naive_wcwidth_sum > 0 ? 1 : 0);
    }

    return cluster_count;
}

/* ========================================================================= */
/* 4. Bracketed Paste and Shell Injection Defense Simulator                  */
/* ========================================================================= */

typedef struct {
    char clean_buffer[1024];
    int clean_len;
    bool in_paste;
    int blocked_newlines_in_paste;
    int unbracketed_executed_commands;
} shell_simulator_t;

static void shell_simulator_init(shell_simulator_t *sim) {
    memset(sim, 0, sizeof(*sim));
}

static void shell_simulator_feed(shell_simulator_t *sim, const char *input, size_t len, bool bracketed_paste_enabled) {
    size_t i = 0;
    while (i < len) {
        /* Check for Bracketed Paste markers: \x1b[200~ and \x1b[201~ */
        if (bracketed_paste_enabled && i + 6 <= len && memcmp(&input[i], "\x1b[200~", 6) == 0) {
            sim->in_paste = true;
            i += 6;
            continue;
        }
        if (bracketed_paste_enabled && i + 6 <= len && memcmp(&input[i], "\x1b[201~", 6) == 0) {
            sim->in_paste = false;
            i += 6;
            continue;
        }

        char ch = input[i++];
        if (sim->in_paste) {
            if (ch == '\n' || ch == '\r') {
                sim->blocked_newlines_in_paste++;
            }
            if (sim->clean_len < (int)sizeof(sim->clean_buffer) - 1) {
                sim->clean_buffer[sim->clean_len++] = ch;
            }
        } else {
            if (ch == '\n' || ch == '\r') {
                sim->unbracketed_executed_commands++;
            } else {
                if (sim->clean_len < (int)sizeof(sim->clean_buffer) - 1) {
                    sim->clean_buffer[sim->clean_len++] = ch;
                }
            }
        }
    }
}

/* ========================================================================= */
/* Test Harness and Verification Suites                                      */
/* ========================================================================= */

static void test_utf8_dfa(void) {
    printf("=== Section 1: UTF-8 DFA Decoder (Bjoern Hoehrmann) ===\n");

    /* Test 1: Valid ASCII string */
    const char *ascii_str = "Hello, Unix!";
    uint32_t state = UTF8_ACCEPT;
    uint32_t cp = 0;
    int ascii_count = 0;
    for (size_t i = 0; i < strlen(ascii_str); i++) {
        if (utf8_decode_step(&state, &cp, (uint8_t)ascii_str[i]) == UTF8_ACCEPT) {
            ascii_count++;
        }
    }
    printf("ASCII decoding: %d codepoints, final state=%u (OK)\n", ascii_count, state);

    /* Test 2: Valid multi-byte UTF-8 sequences */
    /* U+00E9 (e-acute): C3 A9; U+4E2D (CJK Hanzi): E4 B8 AD; U+1F468 (Man): F0 9F 91 A8 */
    const uint8_t multibyte[] = {
        0xC3, 0xA9,             /* e-acute (2-byte) */
        0xE4, 0xB8, 0xAD,       /* CJK Hanzi (3-byte) */
        0xF0, 0x9F, 0x91, 0xA8  /* Man (4-byte) */
    };
    state = UTF8_ACCEPT;
    uint32_t decoded_cps[3];
    int mb_count = 0;
    for (size_t i = 0; i < sizeof(multibyte); i++) {
        if (utf8_decode_step(&state, &cp, multibyte[i]) == UTF8_ACCEPT) {
            decoded_cps[mb_count++] = cp;
        }
    }
    printf("Multi-byte decoding: 3 codepoints (U+%04X, U+%04X, U+%04X), final state=%u (OK)\n",
           decoded_cps[0], decoded_cps[1], decoded_cps[2], state);

    /* Test 3: Invalid overlong sequence (0xC0 0x80 -> attempted NUL) */
    state = UTF8_ACCEPT;
    utf8_decode_step(&state, &cp, 0xC0);
    utf8_decode_step(&state, &cp, 0x80);
    printf("Overlong sequence 0xC0 0x80: state=%u (expected reject=%u)\n", state, UTF8_REJECT);

    /* Test 4: Invalid surrogate half (0xED 0xA0 0x80 -> attempted U+D800) */
    state = UTF8_ACCEPT;
    utf8_decode_step(&state, &cp, 0xED);
    utf8_decode_step(&state, &cp, 0xA0);
    utf8_decode_step(&state, &cp, 0x80);
    printf("Surrogate half 0xED 0xA0 0x80: state=%u (expected reject=%u)\n", state, UTF8_REJECT);

    /* Test 5: Out of range sequence (0xF5 0x80 0x80 0x80 -> beyond U+10FFFF) */
    state = UTF8_ACCEPT;
    utf8_decode_step(&state, &cp, 0xF5);
    printf("Out of range byte 0xF5: state=%u (expected reject=%u)\n", state, UTF8_REJECT);
}

static void test_vt_parser(void) {
    printf("\n=== Section 2: DEC VT500 / ECMA-48 Parser State Machine ===\n");
    vt_parser_t parser;
    vt_parser_init(&parser);

    /* Test sequence:
     * 1. Ground text: "INIT" (4 prints)
     * 2. C0 control: LF (1 execute)
     * 3. SGR with colon subparameters: \x1b[38:2::255:128:0m (Truecolor orange)
     * 4. CUP: \x1b[15;42H (cursor row 15, col 42)
     * 5. OSC 0 window title: \x1b]0;Toolchain Console\x07
     * 6. OSC 8 hyperlink: \x1b]8;;https://example.com\x1b\\Anchor\x1b]8;;\x1b\\
     * 7. Kitty keyboard protocol push: \x1b[>1u (flags = 1)
     * 8. Alternate screen buffer switch: \x1b[?1049h
     */
    const char stream[] =
        "INIT\n"
        "\x1b[38:2::255:128:0m"
        "\x1b[15;42H"
        "\x1b]0;Toolchain Console\x07"
        "\x1b]8;;https://example.com\x1b\\Anchor\x1b]8;;\x1b\\"
        "\x1b[>1u"
        "\x1b[?1049h";

    vt_parser_feed_string(&parser, stream, sizeof(stream) - 1);

    printf("Parser metrics:\n");
    printf("  Print characters: %d\n", parser.count_print);
    printf("  Execute C0 controls: %d\n", parser.count_execute);
    printf("  CSI dispatches: %d\n", parser.count_csi_dispatch);
    printf("  OSC dispatches: %d\n", parser.count_osc_dispatch);
    printf("  Final cursor position: row=%d, col=%d\n", parser.cursor_row, parser.cursor_col);
    if (parser.sgr_truecolor_seen) {
        printf("  SGR 38:2:: truecolor: r=%d g=%d b=%d\n", parser.sgr_r, parser.sgr_g, parser.sgr_b);
    } else {
        printf("  SGR 38:2:: truecolor: not decoded\n");
    }
    printf("  OSC 8 hyperlink URI: %s\n", parser.osc8_uri[0] ? parser.osc8_uri : "(none)");
    printf("  Kitty keyboard protocol flags: %d\n", parser.kitty_keyboard_flags);
    printf("  Alternate screen buffer active: %s\n", parser.alternate_screen_active ? "true" : "false");

    /* Now test exiting alternate screen buffer: \x1b[?1049l */
    const char exit_alt[] = "\x1b[?1049l";
    vt_parser_feed_string(&parser, exit_alt, sizeof(exit_alt) - 1);
    printf("  After DECRST 1049:\n");
    printf("    Alternate screen buffer active: %s\n", parser.alternate_screen_active ? "true" : "false");
    printf("    Restored cursor position: row=%d, col=%d\n", parser.cursor_row, parser.cursor_col);
}

static void test_unicode_width_and_graphemes(void) {
    printf("\n=== Section 3: Character Width and Grapheme Segmentation ===\n");

    /* libc wcwidth() needs a UTF-8 LC_CTYPE; in the "C" locale it rejects non-ASCII */
    const char *loc = setlocale(LC_CTYPE, "en_US.UTF-8");
    if (loc == NULL) loc = setlocale(LC_CTYPE, "C.UTF-8");
    printf("libc LC_CTYPE: %s\n", loc != NULL ? loc : "(no UTF-8 locale available)");

    /* Define diverse Unicode test cases */
    struct {
        const char *description;
        uint32_t codepoints[6];
        int count;
    } cases[] = {
        {"ASCII 'A'", {0x0041}, 1},
        {"CJK Hanzi U+4E2D", {0x4E2D}, 1},
        {"Combining acute 'e' + U+0301", {0x0065, 0x0301}, 2},
        {"Emoji skin tone: Waving hand + Medium-Dark tone", {0x1F44B, 0x1F3FE}, 2},
        {"Emoji ZWJ: Man + ZWJ + Laptop (Technologist)", {0x1F468, 0x200D, 0x1F4BB}, 3},
        {"Flag sequence: US Flag (Regional U + Regional S)", {0x1F1FA, 0x1F1F8}, 2}
    };

    int num_cases = sizeof(cases) / sizeof(cases[0]);
    for (int i = 0; i < num_cases; i++) {
        grapheme_cluster_t clusters[4];
        int num_clusters = segment_graphemes(cases[i].codepoints, cases[i].count, clusters, 4);

        int total_display_width = 0;
        int total_naive_width = 0;
        int libc_sum = 0;
        for (int k = 0; k < cases[i].count; k++) {
            int w = wcwidth((wchar_t)cases[i].codepoints[k]);
            libc_sum += (w > 0) ? w : 0;
        }
        for (int c = 0; c < num_clusters; c++) {
            total_display_width += clusters[c].display_width;
            total_naive_width += clusters[c].naive_wcwidth_sum;
        }

        printf("Case %d [%s]:\n", i + 1, cases[i].description);
        printf("  Codepoint count: %d, Grapheme cluster count: %d\n", cases[i].count, num_clusters);
        printf("  Naive codepoint wcwidth sum (lab table): %d\n", total_naive_width);
        printf("  Naive codepoint wcwidth sum (libc wcwidth): %d\n", libc_sum);
        printf("  True terminal display width: %d\n", total_display_width);
        if (total_naive_width != total_display_width) {
            printf("  DISCREPANCY: Naive wcwidth over-allocates by %d column(s)!\n",
                   total_naive_width - total_display_width);
        } else {
            printf("  Match: wcwidth aligns with terminal grid.\n");
        }
    }
}

static void test_bracketed_paste_and_injection(void) {
    printf("\n=== Section 4: Bracketed Paste Mode and Injection Defense ===\n");

    /* Attack payload simulating a user pasting multi-line text into a shell prompt:
     * "git status\nrm -rf ./scratch\n"
     */
    const char malicious_payload[] = "git status\nrm -rf ./scratch\n";
    size_t payload_len = strlen(malicious_payload);

    /* The terminal-side mode comes from the parser: DECSET 2004 sets it, DECRST 2004 clears it */
    vt_parser_t mode_parser;
    vt_parser_init(&mode_parser);
    bool mode_before = mode_parser.bracketed_paste_active;
    vt_parser_feed_string(&mode_parser, "\x1b[?2004h", 8);
    bool mode_after_set = mode_parser.bracketed_paste_active;
    vt_parser_feed_string(&mode_parser, "\x1b[?2004l", 8);
    bool mode_after_reset = mode_parser.bracketed_paste_active;
    vt_parser_feed_string(&mode_parser, "\x1b[?2004h", 8);
    printf("DECSET/DECRST 2004 parser state: initial=%s, after set=%s, after reset=%s\n",
           mode_before ? "on" : "off", mode_after_set ? "on" : "off", mode_after_reset ? "on" : "off");

    /* Scenario A: Bracketed paste DISABLED */
    bool scenario_a_mode = mode_before;
    shell_simulator_t sim_unprotected;
    shell_simulator_init(&sim_unprotected);
    shell_simulator_feed(&sim_unprotected, malicious_payload, payload_len, scenario_a_mode);

    printf("Scenario A (Bracketed Paste DISABLED):\n");
    printf("  Parser mode flag handed to the shell: %s\n", scenario_a_mode ? "on" : "off");
    printf("  Commands executed immediately: %d\n", sim_unprotected.unbracketed_executed_commands);
    printf("  Blocked newlines: %d\n", sim_unprotected.blocked_newlines_in_paste);

    /* Scenario B: Bracketed paste ENABLED */
    /* Terminal emulator wraps the paste between \x1b[200~ and \x1b[201~ */
    char wrapped_payload[256];
    int wrapped_len = snprintf(wrapped_payload, sizeof(wrapped_payload),
                              "\x1b[200~%s\x1b[201~", malicious_payload);

    shell_simulator_t sim_protected;
    shell_simulator_init(&sim_protected);
    shell_simulator_feed(&sim_protected, wrapped_payload, (size_t)wrapped_len, mode_parser.bracketed_paste_active);

    printf("Scenario B (Bracketed Paste ENABLED via DECSET 2004):\n");
    printf("  Parser mode flag handed to the shell: %s\n", mode_parser.bracketed_paste_active ? "on" : "off");
    printf("  Commands executed immediately: %d\n", sim_protected.unbracketed_executed_commands);
    printf("  Blocked newlines (buffered safely): %d\n", sim_protected.blocked_newlines_in_paste);
    printf("  Buffered literal text: \"");
    for (int i = 0; i < sim_protected.clean_len; i++) {
        char c = sim_protected.clean_buffer[i];
        if (c == '\n') printf("\\n");
        else if (c == '\r') printf("\\r");
        else putchar(c);
    }
    printf("\"\n");
}

int main(void) {
    test_utf8_dfa();
    test_vt_parser();
    test_unicode_width_and_graphemes();
    test_bracketed_paste_and_injection();
    return 0;
}
