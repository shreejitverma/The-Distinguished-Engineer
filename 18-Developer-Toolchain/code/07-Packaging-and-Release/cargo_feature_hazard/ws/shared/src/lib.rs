//! Frame checksum shared by the firmware and the host tool.
//!
//! The crate is always `no_std`; the additive `std` feature links std back in
//! and adds host-only helpers, as the Cargo Book's features chapter recommends.
#![no_std]

#[cfg(feature = "std")]
extern crate std;

/// True when this build of `shared` was compiled with its `std` feature.
/// Downstream crates read it in a const context to detect a feature leak.
pub const STD_ENABLED: bool = cfg!(feature = "std");

/// Fletcher-16 over a frame: allocation-free, so it runs on the target.
pub fn checksum(data: &[u8]) -> u16 {
    let (mut a, mut b) = (0u16, 0u16);
    for &byte in data {
        a = (a + u16::from(byte)) % 255;
        b = (b + a) % 255;
    }
    (b << 8) | a
}

/// Host-only helper: needs an allocator, so it exists only with `std`.
#[cfg(feature = "std")]
pub fn checksum_hex(data: &[u8]) -> std::string::String {
    std::format!("{:04x}", checksum(data))
}
