#[unsafe(no_mangle)]
pub extern "C" fn ext_answer() -> i32 {
    if cfg!(feature = "fast") { 2 } else { 1 }
}
