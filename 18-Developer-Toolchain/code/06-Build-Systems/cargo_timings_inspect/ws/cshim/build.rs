// Compiles a C file into a static archive without the cc crate, so the lab
// needs no registry access. This is the build-script half of the C handoff.
use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let cc = env::var("CC").unwrap_or_else(|_| "cc".to_string());
    let ar = env::var("AR").unwrap_or_else(|_| "ar".to_string());
    let obj = out.join("shim.o");
    let status = Command::new(&cc)
        .args(["-c", "-O2", "-fPIC", "-Icsrc", "csrc/shim.c", "-o"])
        .arg(&obj)
        .status()
        .expect("failed to run the C compiler");
    assert!(status.success(), "C compiler failed");
    let lib = out.join("libshim.a");
    // OUT_DIR persists across reruns, so remove the old archive first.
    let _ = std::fs::remove_file(&lib);
    let status = Command::new(&ar).arg("crs").arg(&lib).arg(&obj).status().expect("failed to run ar");
    assert!(status.success(), "ar failed");
    let include = env::current_dir().expect("cwd").join("csrc");
    println!("cargo::rustc-link-search=native={}", out.display());
    println!("cargo::rustc-link-lib=static=shim");
    println!("cargo::metadata=include={}", include.display());
    println!("cargo::rerun-if-changed=csrc/shim.c");
    println!("cargo::rerun-if-changed=csrc/shim.h");
    println!("cargo::rerun-if-changed=build.rs");
    println!("cargo::rerun-if-env-changed=CC");
}
