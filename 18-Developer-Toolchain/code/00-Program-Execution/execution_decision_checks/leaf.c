/* leaf makes no call and needs no stack; leaf_stack makes no call but needs stack memory. */
int leaf(int x) { return x * 3 + 1; }

int leaf_stack(int i) {
    volatile int buf[8];
    for (int k = 0; k < 8; k++) buf[k] = k * i;
    return buf[i & 7];
}

/* nonleaf calls out, so it needs a frame record whatever the leaf flags say. */
extern void sink(int);
int nonleaf(int x) {
    sink(x);
    return x + 1;
}
