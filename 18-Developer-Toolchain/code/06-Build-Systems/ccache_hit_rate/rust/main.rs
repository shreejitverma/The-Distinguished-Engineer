//! A binary crate: rustc invokes the system linker, so sccache refuses to
//! cache it.

fn main() {
    println!("{}", 14 * 3);
}
