// Small functions whose checks the lab inspects in trap mode.
extern "C" int add(int a, int b) { return a + b; }
extern "C" int mul(int a, int b) { return a * b; }
extern "C" int sdiv(int a, int b) { return a / b; }
extern "C" int index4(int i) {
    static const int arr[4] = {1, 2, 3, 4};
    return arr[i];
}
extern "C" int two_adds_one_mul(int a, int b, int c) {
    int x = a + b;
    int y = x + c;
    return x * y;
}
