unsafe extern "C" {
    fn shim_add(a: i32, b: i32) -> i32;
}

/// Safe wrapper over the C function compiled by build.rs.
pub fn add(a: i32, b: i32) -> i32 {
    // SAFETY: shim_add takes two ints by value and has no preconditions.
    unsafe { shim_add(a, b) }
}
