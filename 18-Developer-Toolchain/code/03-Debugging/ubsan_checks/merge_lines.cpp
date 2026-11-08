// Three additions on three lines; at -O2 they share one trap.
extern "C" int three_adds(int a, int b, int c, int d) {
    int x = a + b;
    int y = x + c;
    int z = y + d;
    return z;
}
