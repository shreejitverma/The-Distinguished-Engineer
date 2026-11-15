/// Multiplies two ints; formatting the result pulls in std, so the C++ link
/// needs the native libraries that `--print native-static-libs` reports.
#[unsafe(no_mangle)]
pub extern "C" fn rs_mul_len(a: i32, b: i32) -> i32 {
    format!("{}", a * b).len() as i32 * 100 + a * b
}
