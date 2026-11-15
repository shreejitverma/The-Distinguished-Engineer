/// Reports which features this copy of the crate was compiled with.
pub fn features() -> &'static str {
    match (cfg!(feature = "alpha"), cfg!(feature = "beta")) {
        (true, true) => "alpha+beta",
        (true, false) => "alpha",
        (false, true) => "beta",
        (false, false) => "none",
    }
}
