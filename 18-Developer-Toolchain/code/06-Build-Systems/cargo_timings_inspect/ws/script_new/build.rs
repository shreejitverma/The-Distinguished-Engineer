// New-style build script: only the listed inputs can trigger a rerun.
fn main() {
    let flavor = std::env::var("LAB_FLAVOR").unwrap_or_else(|_| "plain".to_string());
    println!("cargo::rustc-env=SCRIPT_NEW_FLAVOR={flavor}");
    println!("cargo::rerun-if-changed=build.rs");
    println!("cargo::rerun-if-env-changed=LAB_FLAVOR");
}
