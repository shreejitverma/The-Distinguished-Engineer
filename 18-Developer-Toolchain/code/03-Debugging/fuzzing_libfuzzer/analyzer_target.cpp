// analyzer_target.cpp - four functions for the Clang Static Analyzer.
// Three hold path-dependent bugs that -Wall -Wextra cannot see; one is correct
// code that a path-insensitive checker would flag.

struct Order {
    int qty;
};

// Returns nullptr when id is out of range.
Order *find_order(Order *book, int n, int id) {
    if (id < 0 || id >= n) return nullptr;
    return &book[id];
}

// Bug 1 (inter-procedural): the analyzer inlines find_order and follows its nullptr path.
int fill_qty(Order *book, int n, int id) {
    Order *o = find_order(book, n, id);
    return o->qty;
}

// Correct: q is null only when have is false, and then it is never dereferenced.
// Path sensitivity keeps the two branches correlated, so no report.
int guarded(int *p, bool have) {
    int *q = have ? p : nullptr;
    if (have) return *q;
    return 0;
}

// Bug 2: the early return leaks buf.
int sum_small(int n) {
    int *buf = new int[n > 0 ? n : 1];
    if (n > 100) return -1;
    int s = 0;
    for (int i = 0; i < n; ++i) s += buf[i] = i;
    delete[] buf;
    return s;
}

// Bug 3: on the path where every lot is empty, count stays 0.
int average_lot(const int *lots, int n) {
    int total = 0;
    int count = 0;
    for (int i = 0; i < n; ++i) {
        if (lots[i] > 0) {
            total += lots[i];
            ++count;
        }
    }
    return total / count;
}
