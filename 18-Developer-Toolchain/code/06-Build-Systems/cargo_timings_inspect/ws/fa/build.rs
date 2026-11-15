fn main() {
    // The build script links the build-dependency copy of `common`.
    println!("cargo::rustc-env=FA_BUILD_SCRIPT_SAW={}", common::features());
    println!("cargo::rerun-if-changed=build.rs");
}
