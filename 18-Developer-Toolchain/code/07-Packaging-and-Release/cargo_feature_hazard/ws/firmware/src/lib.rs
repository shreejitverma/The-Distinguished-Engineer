//! Firmware logic that must link a `no_std` build of `shared`.
#![no_std]

// Tripwire. On a real bare-metal target (`target_os = "none"`) a leaked `std`
// feature fails anyway with E0463, because `shared` then says `extern crate std`
// and the target has no std. The lab runs on the host, where std exists, so it
// passes `--cfg firmware_target` to stand in for the bare-metal target.
#[cfg(any(target_os = "none", firmware_target))]
const _: () = assert!(
    !shared::STD_ENABLED,
    "feature leak: shared was compiled with std for the firmware build"
);

/// Checksum of one radio frame.
pub fn frame_checksum(frame: &[u8]) -> u16 {
    shared::checksum(frame)
}

#[cfg(test)]
mod tests {
    // Host tests use the std-only helper, which is why the manifest lists
    // shared again under [dev-dependencies] with features = ["std"].
    #[test]
    fn known_vector() {
        assert_eq!(shared::checksum_hex(b"abcde"), "c8f0");
        assert_eq!(super::frame_checksum(b"abcde"), 0xc8f0);
    }
}
