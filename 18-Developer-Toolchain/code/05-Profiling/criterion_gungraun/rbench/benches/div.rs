// Divan 0.1.21 benchmark with AllocProfiler: counts allocator calls per iteration.

use divan::AllocProfiler;

#[global_allocator]
static ALLOC: AllocProfiler = AllocProfiler::system();

fn main() {
    divan::main();
}

#[divan::bench(args = [1000], sample_count = 20)]
fn push_grow(n: u64) -> Vec<u64> {
    rbench::push_grow(divan::black_box(n))
}

#[divan::bench(args = [1000], sample_count = 20)]
fn push_reserved(n: u64) -> Vec<u64> {
    rbench::push_reserved(divan::black_box(n))
}
