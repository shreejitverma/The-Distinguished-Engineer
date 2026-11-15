// DEP_SHIM_INCLUDE comes from cshim's `cargo::metadata=include=...`, keyed by
// its `links = "shim"` value, and reaches only immediate dependents.
fn main() {
    let seen = std::env::var("DEP_SHIM_INCLUDE")
        .map(|dir| std::path::Path::new(&dir).join("shim.h").exists())
        .unwrap_or(false);
    println!("cargo::rustc-env=SHIM_HEADER_SEEN={}", if seen { "yes" } else { "no" });
    println!("cargo::rerun-if-env-changed=DEP_SHIM_INCLUDE");
}
