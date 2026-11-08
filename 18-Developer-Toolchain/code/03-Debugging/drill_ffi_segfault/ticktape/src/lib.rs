//! ticktape: a zero-copy cursor over a market-data buffer handed in from Python.
//!
//! The planted bug: `tape_feed` keeps a raw pointer into a Python `bytes`
//! object after the call returns, but takes no reference on that object.
//! Python's reference count, not Rust, decides when the memory dies, so every
//! later `tape_sum` reads memory whose lifetime Rust never controlled.
//! `tape_feed_copy` is the fix: it copies the bytes into memory Rust owns.

use std::ptr;

pub struct Tape {
    data: *const u8,
    len: usize,
    // Keeps the copied buffer alive for `tape_feed_copy`; `None` for the borrowed path.
    owned: Option<Box<[u8]>>,
}

#[unsafe(no_mangle)]
pub extern "C" fn tape_new() -> *mut Tape {
    Box::into_raw(Box::new(Tape {
        data: ptr::null(),
        len: 0,
        owned: None,
    }))
}

/// BUG: stores a borrowed pointer that outlives the call.
///
/// # Safety
/// `t` must come from `tape_new`, and `data` must stay valid for `len` bytes
/// for as long as the tape is read. A Python caller cannot promise the second
/// condition without keeping the `bytes` object alive itself.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tape_feed(t: *mut Tape, data: *const u8, len: usize) {
    let t = unsafe { &mut *t };
    t.owned = None;
    t.data = data;
    t.len = len;
}

/// FIX: copies the bytes, so the tape no longer depends on the Python object.
///
/// # Safety
/// `t` must come from `tape_new`, and `data` must be valid for `len` bytes
/// for the duration of this call only.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tape_feed_copy(t: *mut Tape, data: *const u8, len: usize) {
    assert!(!data.is_null(), "tape_feed_copy: null data");
    let t = unsafe { &mut *t };
    let owned: Box<[u8]> = unsafe { std::slice::from_raw_parts(data, len) }.into();
    t.data = owned.as_ptr();
    t.len = owned.len();
    t.owned = Some(owned);
}

/// The address the tape will read from, so the driver can tell the debugger where to look.
///
/// # Safety
/// `t` must come from `tape_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tape_data_ptr(t: *const Tape) -> usize {
    unsafe { (*t).data as usize }
}

/// Sums every byte with one volatile 1-byte load each, so the first touch of
/// freed memory is the first byte, and the compiler cannot widen or skip loads.
///
/// # Safety
/// `t` must come from `tape_new` and its buffer must still be alive.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tape_sum(t: *const Tape) -> u64 {
    let t = unsafe { &*t };
    let mut sum = 0u64;
    for i in 0..t.len {
        sum += u64::from(unsafe { ptr::read_volatile(t.data.add(i)) });
    }
    sum
}

/// # Safety
/// `t` must come from `tape_new` and must not be used afterwards.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn tape_free(t: *mut Tape) {
    if !t.is_null() {
        drop(unsafe { Box::from_raw(t) });
    }
}
