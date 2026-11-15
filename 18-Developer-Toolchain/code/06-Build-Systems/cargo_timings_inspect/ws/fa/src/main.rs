fn main() { println!("runtime common={} build-script common={}", common::features(), env!("FA_BUILD_SCRIPT_SAW")); }
