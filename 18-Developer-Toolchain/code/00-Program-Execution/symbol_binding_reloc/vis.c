__attribute__((visibility("default"))) int sym_default(void) {
    return 100;
}

__attribute__((visibility("hidden"))) int sym_hidden(void) {
    return 200;
}

__attribute__((visibility("protected"))) int sym_protected(void) {
    return 300;
}
