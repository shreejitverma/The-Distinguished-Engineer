// dhat 0.3.3 heap usage testing. One measurement per process, because dhat allows
// only one Profiler at a time and keeps global state.
//
//   heap grow      -> stats for rbench::push_grow(1000)
//   heap reserved  -> stats for rbench::push_reserved(1000)
//   heap assert    -> a dhat::assert_eq! that must fail and save dhat-heap.json

use std::hint::black_box;

#[global_allocator]
static ALLOC: dhat::Alloc = dhat::Alloc;

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    let _profiler = dhat::Profiler::builder().testing().build();

    let v = match mode.as_str() {
        "grow" | "assert" => rbench::push_grow(black_box(1000)),
        "reserved" => rbench::push_reserved(black_box(1000)),
        other => panic!("unknown mode {other:?}"),
    };
    black_box(&v);
    drop(v);

    let stats = dhat::HeapStats::get();
    if mode == "assert" {
        // Wrong on purpose: push_grow makes 9 blocks (1 alloc + 8 reallocs), not 1.
        dhat::assert_eq!(stats.total_blocks, 1);
    }
    println!(
        "total_blocks={} total_bytes={} max_blocks={} max_bytes={} curr_blocks={} curr_bytes={}",
        stats.total_blocks,
        stats.total_bytes,
        stats.max_blocks,
        stats.max_bytes,
        stats.curr_blocks,
        stats.curr_bytes
    );
}
