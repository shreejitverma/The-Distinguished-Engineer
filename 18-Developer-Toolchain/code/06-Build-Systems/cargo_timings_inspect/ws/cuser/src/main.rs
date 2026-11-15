fn main() { println!("shim_add(2, 3) = {}, header seen by build script: {}", cshim::add(2, 3), env!("SHIM_HEADER_SEEN")); }
