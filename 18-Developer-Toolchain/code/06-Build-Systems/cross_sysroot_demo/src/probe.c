/* Freestanding translation unit: needs no headers and no libc, so it compiles for any target clang knows. */
int probe(int x) { return x + 1; }
