// Old-style build script: no rerun-if directives, so Cargo reruns it
// whenever any file in the package changes.
fn main() {
    println!("cargo::rustc-env=SCRIPT_OLD_RAN=1");
}
