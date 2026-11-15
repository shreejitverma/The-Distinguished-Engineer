// Code generator for routes.rs, standing in for a Cargo build script.
// std::collections::HashMap uses RandomState, which seeds SipHash with fresh random
// keys per process, so iterating it yields a different order in every run.
// With --sorted the entries are sorted before they are written.
use std::collections::HashMap;
use std::env;
use std::fmt::Write;
use std::fs;

const NAMES: [&str; 12] = [
    "quotes", "orders", "fills", "cancels", "positions", "risk",
    "heartbeat", "snapshot", "replay", "audit", "admin", "metrics",
];

fn main() {
    let args: Vec<String> = env::args().collect();
    let out = &args[1];
    let sorted = args.iter().skip(2).any(|a| a == "--sorted");

    let mut routes: HashMap<&str, u16> = HashMap::new();
    for (i, name) in NAMES.iter().enumerate() {
        routes.insert(name, 8000 + i as u16);
    }
    let mut entries: Vec<(&str, u16)> = routes.into_iter().collect();
    if sorted {
        entries.sort();
    }

    let mut s = String::new();
    writeln!(s, "pub const ROUTES: [(&str, u16); {}] = [", entries.len()).unwrap();
    for (name, port) in &entries {
        writeln!(s, "    ({:?}, {}),", name, port).unwrap();
    }
    writeln!(s, "];").unwrap();
    writeln!(s, "#[inline(never)]").unwrap();
    writeln!(s, "pub fn route_port(i: usize) -> u16 {{ ROUTES[i].1 }}").unwrap();
    fs::write(out, s).unwrap();
}
