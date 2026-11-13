// Gungraun 0.20.0 library benchmarks: one Callgrind run per bench, instruction counts.
// Linux only (Valgrind); on other targets this file builds to a stub that says so.
// The Linux items follow the guide's layout exactly: attribute macros at top level and
// `main!` inside `fn main`.

#[cfg(target_os = "linux")]
use gungraun::prelude::*;
#[cfg(target_os = "linux")]
use std::hint::black_box;

#[cfg(target_os = "linux")]
fn make_data(n: u64) -> Vec<u64> {
    (0..n).collect()
}

#[cfg(target_os = "linux")]
#[library_benchmark]
#[bench::n1000(1000)]
fn push_grow(n: u64) -> Vec<u64> {
    black_box(rbench::push_grow(black_box(n)))
}

#[cfg(target_os = "linux")]
#[library_benchmark]
#[bench::n1000(1000)]
fn push_reserved(n: u64) -> Vec<u64> {
    black_box(rbench::push_reserved(black_box(n)))
}

#[cfg(target_os = "linux")]
#[library_benchmark]
#[bench::n4096(args = [4096], setup = make_data)]
fn checksum(data: Vec<u64>) -> u64 {
    black_box(rbench::checksum(black_box(&data)))
}

#[cfg(target_os = "linux")]
library_benchmark_group!(
    name = rbench_group,
    benchmarks = [push_grow, push_reserved, checksum]
);

#[cfg(target_os = "linux")]
fn main() {
    main!(library_benchmark_groups = rbench_group);
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("gung: Gungraun benchmarks need Valgrind and run only on Linux in this lab");
}
