#include <stdio.h>

/* General Dynamic (GD): default for non-hidden variables in PIC code */
__attribute__((visibility("default"))) __thread int var_gd = 10;

/* Local Dynamic (LD): hidden visibility allows module-level anchor grouping */
__attribute__((visibility("hidden"))) __thread int var_ld1 = 20;
__attribute__((visibility("hidden"))) __thread int var_ld2 = 30;

/* Initial Exec (IE): static TLS model accessing GOT-relative offset */
__attribute__((visibility("default"))) __thread int var_ie __attribute__((tls_model("initial-exec"))) = 40;

/* Local Exec (LE): direct static offset from thread pointer (TP).
   Only valid in the executable, so the shared-object builds define NO_LE. */
#ifndef NO_LE
__attribute__((visibility("hidden"))) __thread int var_le __attribute__((tls_model("local-exec"))) = 50;
#endif

int get_gd(void) {
    return var_gd;
}

int get_ld(void) {
    return var_ld1 + var_ld2;
}

int get_ie(void) {
    return var_ie;
}

#ifndef NO_LE
int get_le(void) {
    return var_le;
}
#endif
