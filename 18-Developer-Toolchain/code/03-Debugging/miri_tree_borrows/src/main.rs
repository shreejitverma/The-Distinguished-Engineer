//! Eleven small programs, each with one memory, aliasing, or concurrency bug (or none, under one
//! aliasing model).
//!
//! `run.sh` runs the cases natively, under Miri with Stacked Borrows (the default) and with
//! Tree Borrows (`-Zmiri-tree-borrows`), under AddressSanitizer, and the two threaded cases under
//! ThreadSanitizer, then prints which tool reported what.
//! Ten of the cases compile without errors and run to completion natively, which is the point:
//! undefined behavior is invisible until a checker models the rule that was broken.
//! The eleventh, `unchecked_index`, is the exception that the standard library's own debug
//! assertions catch, and `run.sh` gives it a section of its own.

use std::mem::MaybeUninit;

/// A `&mut` is created from a raw pointer, the raw parent is read, then the `&mut` is written.
/// Stacked Borrows: the read through the parent pops the `Unique` item, so the write is UB.
/// Tree Borrows: the new `&mut` starts `Reserved`, which tolerates foreign reads, so it is fine.
fn reserved_foreign_read() -> i32 {
    let mut x = 0i32;
    let base: *mut i32 = &mut x;
    let r: &mut i32 = unsafe { &mut *base };
    let seen = unsafe { base.read() };
    *r = 7;
    seen + x
}

/// Same shape, but the parent is written instead of read before the `&mut` is used.
/// A foreign write disables a `Reserved` reference, so this is UB under both models.
fn reserved_foreign_write() -> i32 {
    let mut x = 0i32;
    let base: *mut i32 = &mut x;
    let r: &mut i32 = unsafe { &mut *base };
    unsafe { base.write(1) };
    *r = 7;
    x
}

/// The callee holds a `&mut` argument, which is protected for the duration of the call, while
/// the caller's raw alias is written from inside the call.
/// Both models reject the write itself, before the protected reference is ever used again.
fn protected_foreign_write() -> i32 {
    fn callee(r: &mut i32, alias: *mut i32) -> i32 {
        unsafe { alias.write(3) };
        *r
    }
    let mut x = 0i32;
    let base: *mut i32 = &mut x;
    callee(unsafe { &mut *base }, base)
}

/// A raw pointer derived from `&mut arr[0]` writes `arr[1]`.
/// Stacked Borrows retags only the 1-byte range of the reference, so offset 1 has no item: UB.
/// Tree Borrows tracks out-of-range locations lazily, so the access is allowed.
fn write_past_element() -> u8 {
    let mut arr = [0u8; 2];
    let p: *mut u8 = &mut arr[0];
    unsafe { p.add(1).write(9) };
    arr[1]
}

/// Writing through a pointer derived from a shared reference: UB under both models.
fn write_through_shared() -> i32 {
    let x = 1i32;
    let r = &x;
    let p = r as *const i32 as *mut i32;
    unsafe { p.write(2) };
    x
}

/// Classic heap use-after-free: independent of any aliasing model.
fn use_after_free() -> i32 {
    let b = Box::new(41i32);
    let p: *const i32 = &*b;
    drop(b);
    unsafe { p.read() + 1 }
}

/// Reading an uninitialized integer: Miri reports it, AddressSanitizer cannot see it.
#[allow(invalid_value)]
fn uninit_read() -> i32 {
    let m = MaybeUninit::<i32>::uninit();
    let v = unsafe { m.assume_init() };
    if v == 0 { 0 } else { 1 }
}

/// A heap allocation that is never freed and not reachable from a static.
fn leak() -> i32 {
    let b = Box::new(5i32);
    let v = *b;
    std::mem::forget(b);
    v
}

/// Two unsynchronized non-atomic increments of a `static mut` from two threads.
fn data_race() -> i32 {
    static mut COUNTER: i32 = 0;
    let t = std::thread::spawn(|| unsafe { COUNTER += 1 });
    unsafe { COUNTER += 1 };
    t.join().unwrap();
    unsafe { COUNTER }
}

/// Two threads each do an atomic load and then an atomic store.
/// An update can be lost, but every access is atomic, so there is no data race and no undefined
/// behavior for Miri or ThreadSanitizer to report; the Loom lab next door finds this one.
fn lost_update() -> i32 {
    use std::sync::atomic::{AtomicI32, Ordering};
    static N: AtomicI32 = AtomicI32::new(0);
    let t = std::thread::spawn(|| {
        let v = N.load(Ordering::SeqCst);
        N.store(v + 1, Ordering::SeqCst);
    });
    let v = N.load(Ordering::SeqCst);
    N.store(v + 1, Ordering::SeqCst);
    t.join().unwrap();
    N.load(Ordering::SeqCst)
}

/// `get_unchecked` one past the end of a three-element heap slice.
/// The standard library asserts this precondition when the calling crate has debug assertions on.
fn unchecked_index() -> i32 {
    let v = vec![1u8, 2, 3];
    let i = std::hint::black_box(3usize);
    i32::from(unsafe { *v.get_unchecked(i) })
}

fn main() {
    let case = std::env::args().nth(1).unwrap_or_default();
    let v: i64 = match case.as_str() {
        "reserved_foreign_read" => reserved_foreign_read().into(),
        "reserved_foreign_write" => reserved_foreign_write().into(),
        "protected_foreign_write" => protected_foreign_write().into(),
        "write_past_element" => write_past_element().into(),
        "write_through_shared" => write_through_shared().into(),
        "use_after_free" => use_after_free().into(),
        "uninit_read" => uninit_read().into(),
        "leak" => leak().into(),
        "data_race" => data_race().into(),
        "lost_update" => lost_update().into(),
        "unchecked_index" => unchecked_index().into(),
        other => {
            eprintln!("unknown case {other:?}");
            std::process::exit(2);
        }
    };
    // The value is printed so the computation is not optimized away; run.sh never echoes it,
    // because a use-after-free or uninitialized read returns whatever the memory holds.
    // run.sh does require this line, so that "exit 0" always means the case ran to its end.
    println!("{case} returned {v}");
}
