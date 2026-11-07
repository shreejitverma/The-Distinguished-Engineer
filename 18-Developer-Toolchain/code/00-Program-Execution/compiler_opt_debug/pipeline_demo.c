/* pipeline_demo.c - Compiler Optimization and Debuggability Lab
 *
 * Demonstrates the five core compiler optimization passes that degrade debuggability:
 * 1. Inlining: Function call elimination and inline subroutine DIE generation.
 * 2. SROA: Aggregate struct decomposition into scalar registers with DW_OP_piece.
 * 3. Tail Call Elimination: Stack frame omission and DW_TAG_call_site emission.
 * 4. Line Table Reordering: Non-monotonic instruction execution in .debug_line.
 * 5. Vectorization: Induction variable collapse during SIMD execution.
 */

typedef struct {
    int x;
    int y;
} Point;

static inline int compute_distance_sq(Point p) {
    return p.x * p.x + p.y * p.y;
}

int external_sink(int val);

int transform_point(int base_x, int base_y) {
    Point pt;
    pt.x = base_x + 3;
    pt.y = base_y * 2;
    int dist_sq = compute_distance_sq(pt);
    return external_sink(dist_sq);
}

void vector_sum(const float *__restrict__ a, const float *__restrict__ b, float *__restrict__ out, int count) {
    for (int i = 0; i < count; i++) {
        out[i] = a[i] + b[i];
    }
}
