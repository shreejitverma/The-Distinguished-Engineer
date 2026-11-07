int compute(int val) {
    int factor = 3;
    int acc = val * factor;
    if (acc > 50) {
        acc += 10;
    } else {
        acc -= 5;
    }
    return acc;
}

int main(void) {
    int base = 20;
    int res = compute(base);
    return res;
}
