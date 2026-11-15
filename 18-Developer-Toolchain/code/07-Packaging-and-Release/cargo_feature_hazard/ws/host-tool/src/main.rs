//! Host-side tool that prints the checksum of its first argument.
fn main() {
    let input = std::env::args().nth(1).unwrap_or_default();
    println!("{}", shared::checksum_hex(input.as_bytes()));
}
