/* hot.c - the "benchmarked" function; its source never changes between builds. */
int hot_loop(const int *a, int n)
{
    int sum = 0;
    for (int i = 0; i < n; ++i) {
        sum += a[i];
    }
    return sum;
}
