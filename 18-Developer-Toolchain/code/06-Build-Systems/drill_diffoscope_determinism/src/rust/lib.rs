// The generated table is pulled in from OUT_DIR, an absolute path, the way a Cargo
// build script's output is. The bounds check in route_port therefore records the
// generated file's absolute path in its panic Location.
include!(concat!(env!("OUT_DIR"), "/routes.rs"));

pub fn port_of(name: &str) -> Option<u16> {
    ROUTES.iter().position(|(n, _)| *n == name).map(route_port)
}
