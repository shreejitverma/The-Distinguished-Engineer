//! A library crate: rustc emits an rlib without invoking the linker, so
//! sccache can cache it.

pub fn scale(x: u64) -> u64 {
    x * 3
}
