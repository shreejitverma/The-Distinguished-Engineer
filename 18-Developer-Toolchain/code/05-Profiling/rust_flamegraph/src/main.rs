//! Lab program for the note "Rust CPU Profiling: cargo-flamegraph, samply, and Frame Pointers".
//!
//! Modes:
//!   walk        walk the frame-pointer chain from inside an inlined generic function and print
//!               the raw return addresses, then print the frames std::backtrace reports
//!   spin <n>    run the same hot path n times, for a sampling profiler to record
//!
//! run.sh turns the raw addresses into names with atos, so the program itself stays free of
//! any symbolizer and shows exactly what a frame-pointer unwinder has to work with.

use std::hint::black_box;
use std::sync::atomic::{AtomicBool, Ordering};

/// A mixing step; each implementation is a separate monomorphized copy of `checksum`.
pub trait Mix {
    const SEED: u64;
    fn step(acc: u64, x: u64) -> u64;
}

pub struct Fnv;
pub struct Rotl;

impl Mix for Fnv {
    const SEED: u64 = 14_695_981_039_346_656_037;
    #[inline(always)]
    fn step(acc: u64, x: u64) -> u64 {
        (acc ^ x).wrapping_mul(1_099_511_628_211)
    }
}

impl Mix for Rotl {
    const SEED: u64 = 7;
    #[inline(always)]
    fn step(acc: u64, x: u64) -> u64 {
        acc.rotate_left(5) ^ x.wrapping_mul(11_400_714_819_323_198_485)
    }
}

static CAPTURE: AtomicBool = AtomicBool::new(false);

/// Generic and always inlined: it has no machine-code frame of its own in a release build.
#[inline(always)]
fn checksum<M: Mix>(data: &[u64]) -> u64 {
    let mut acc = M::SEED;
    for &x in data {
        acc = M::step(acc, x);
    }
    if CAPTURE.swap(false, Ordering::Relaxed) {
        capture_here();
    }
    acc
}

/// The physical frame that `checksum::<Fnv>` and `checksum::<Rotl>` are inlined into.
#[inline(never)]
fn hot_path(data: &[u64]) -> u64 {
    checksum::<Fnv>(data) ^ checksum::<Rotl>(data)
}

/// A leaf: it calls nothing, so the target's non-leaf policy lets it skip the frame record.
#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lab_leaf_mix(a: u64, b: u64) -> u64 {
    (a ^ b).wrapping_mul(0x9E37_79B9).rotate_left(13)
}

/// A non-leaf: it calls `lab_leaf_mix`, so it must save the link register in a frame record.
#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lab_nonleaf_mix(a: u64, b: u64) -> u64 {
    lab_leaf_mix(black_box(a), b).wrapping_add(lab_leaf_mix(b, black_box(a)))
}

/// Read the current frame pointer register.
#[inline(always)]
fn frame_pointer() -> *const usize {
    let fp: *const usize;
    // SAFETY: reading a register into a local has no side effects.
    unsafe {
        #[cfg(target_arch = "aarch64")]
        core::arch::asm!("mov {}, x29", out(reg) fp, options(nomem, nostack, preserves_flags));
        #[cfg(target_arch = "x86_64")]
        core::arch::asm!("mov {}, rbp", out(reg) fp, options(nomem, nostack, preserves_flags));
    }
    fp
}

/// Walk the chain of frame records: each record is [saved caller fp, return address].
/// This is the whole algorithm a frame-pointer unwinder runs, minus reading another
/// process's memory.
#[inline(never)]
fn walk_frame_pointers() -> Vec<usize> {
    let mut out = Vec::with_capacity(64);
    let mut fp = frame_pointer();
    while !fp.is_null() && out.len() < 64 {
        // SAFETY: on targets that keep frame pointers in non-leaf code, fp points at a valid
        // frame record of a function that is still on this thread's stack; the walk stops at
        // a null fp, a zero return address, or a record that does not move up the stack.
        let (next, ret) = unsafe { (*fp, *fp.add(1)) };
        if ret == 0 {
            break;
        }
        out.push(ret);
        if next <= fp as usize {
            break;
        }
        fp = next as *const usize;
    }
    out
}

#[inline(never)]
fn capture_here() {
    let rets = walk_frame_pointers();
    // The load address lets run.sh undo ASLR for atos; it never reaches the expected output.
    unsafe extern "C" {
        #[link_name = "_mh_execute_header"]
        static MH_EXECUTE_HEADER: u8;
    }
    let load = &raw const MH_EXECUTE_HEADER as usize;
    println!("LOAD {load:#x}");
    for r in &rets {
        // A return address points after the call; one byte back lands inside the call
        // instruction, which is the line and inline scope the caller was in.
        println!("RET {:#x}", r - 1);
    }
    let bt = std::backtrace::Backtrace::force_capture();
    for line in bt.to_string().lines() {
        let line = line.trim_start();
        if let Some((_, name)) = line.split_once(": ") {
            if !line.starts_with("at ") {
                println!("BT {name}");
            }
        }
    }
}

fn workload() -> Vec<u64> {
    (0..4096u64).map(|i| i.wrapping_mul(2_654_435_761) ^ (i >> 3)).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let data = workload();
    match args.get(1).map(String::as_str) {
        Some("walk") => {
            CAPTURE.store(true, Ordering::Relaxed);
            let sum = hot_path(black_box(&data));
            println!("SUM {sum}");
            println!("MIX {}", lab_nonleaf_mix(black_box(3), 5));
        }
        Some("spin") => {
            let n: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(20_000);
            let mut total = 0u64;
            for i in 0..n {
                total = total.wrapping_add(hot_path(black_box(&data)) ^ i);
            }
            println!("SUM {total}");
        }
        _ => {
            eprintln!("usage: rust_flamegraph walk | spin <iterations>");
            std::process::exit(2);
        }
    }
}
