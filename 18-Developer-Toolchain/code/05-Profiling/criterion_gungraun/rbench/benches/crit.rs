// Criterion.rs 0.8.2 wall-clock benchmark of rbench::checksum.
// run.sh saves a baseline with the fast build, then compares the `slow` build to it.

use criterion::{criterion_group, criterion_main, Criterion, Throughput};
use std::hint::black_box;

fn bench_checksum(c: &mut Criterion) {
    let data: Vec<u64> = (0..4096u64).collect();
    let mut group = c.benchmark_group("checksum");
    group.throughput(Throughput::Elements(data.len() as u64));
    group.bench_function("n4096", |b| b.iter(|| rbench::checksum(black_box(&data))));
    group.finish();
}

criterion_group!(benches, bench_checksum);
criterion_main!(benches);
