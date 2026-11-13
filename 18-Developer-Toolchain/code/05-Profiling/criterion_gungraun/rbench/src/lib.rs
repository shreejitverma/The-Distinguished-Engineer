//! Code under test for the Criterion, Divan, dhat, and Gungraun stages of the lab.

use std::hint::black_box;

/// Pushes `n` squares into a `Vec` that starts empty, so it grows by doubling.
/// For 8-byte elements the first allocation holds 4, then 8, 16, ... (RawVec policy).
#[inline(never)]
pub fn push_grow(n: u64) -> Vec<u64> {
    let mut v = Vec::new();
    for i in 0..n {
        v.push(i * i);
    }
    v
}

/// Same output as `push_grow`, but reserves the final capacity up front.
#[inline(never)]
pub fn push_reserved(n: u64) -> Vec<u64> {
    let mut v = Vec::with_capacity(n as usize);
    for i in 0..n {
        v.push(i * i);
    }
    v
}

/// A serial rotate-xor chain over `data`: one pass normally, four with `--features slow`.
/// The loop-carried dependency on `acc` keeps LLVM from vectorizing or merging passes,
/// and `black_box(data)` per pass keeps it from hoisting a pass out of the loop.
#[inline(never)]
pub fn checksum(data: &[u64]) -> u64 {
    let passes: u64 = if cfg!(feature = "slow") { 4 } else { 1 };
    let mut acc = 0u64;
    for p in 0..passes {
        for &x in black_box(data) {
            acc = acc.rotate_left(5) ^ x.wrapping_mul(0x9E37_79B9_7F4A_7C15 ^ p);
        }
    }
    acc
}
