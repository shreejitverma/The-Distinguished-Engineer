// Shared driver, included by each binary after `use kit_<version> as kit;`.
//
// The source is identical for all three library versions. `kit::ingest(frame, &Venue, sink,
// stats)` type-checks against the generic signatures directly and against the `dyn`
// signature through unsizing coercions, so the library is the only variable.

use feedcore::counters;
use feedcore::{
    build_frame, Checksum, Decoder, IngestError, LastPx, Lcg, Range, Sink, Stats, Vwap, HEADER_LEN,
};
use feedcore::{Arca, Asx, Bats, Cme, Eurex, Hkex, Ice, Iex, Lse, Nasdaq, Nyse, Tse};

const FRAMES_PER_VENUE: usize = 64;
const RECORDS_PER_FRAME: usize = 32;
const ROUNDS: usize = 100;
const PASSES: usize = 5;

macro_rules! dispatch_arms {
    ($frame:ident, $sink:ident, $stats:ident; $($venue:ident),*) => {
        match $frame.get(2).copied() {
            $(Some(<$venue as Decoder>::VENUE) => kit::ingest($frame, &$venue, $sink, $stats),)*
            Some(other) => Err(IngestError::UnknownVenue(other)),
            None => Err(IngestError::UnknownVenue(0)),
        }
    };
}

/// Routes a frame to the decoder for its venue byte: 12 call sites of `kit::ingest` per sink type.
#[inline(never)]
fn dispatch<S: Sink>(frame: &[u8], sink: &mut S, stats: &mut Stats) -> Result<usize, IngestError> {
    feedcore::for_each_venue!(dispatch_arms!(frame, sink, stats;))
}

macro_rules! frames_for {
    ($out:ident, $rng:ident, $seq:ident; $($venue:ident),*) => {
        $($out.push(build_frame(
            <$venue as Decoder>::VENUE,
            <$venue as Decoder>::RECORD_LEN,
            $seq,
            RECORDS_PER_FRAME,
            &mut $rng,
        ));)*
    };
}

/// Valid frames, venues interleaved frame by frame the way a consolidated feed arrives.
fn good_frames() -> Vec<Vec<u8>> {
    let mut rng = Lcg(0x5EED_F00D);
    let mut out = Vec::with_capacity(12 * FRAMES_PER_VENUE);
    for i in 0..FRAMES_PER_VENUE {
        let seq = 1 + i as u32;
        feedcore::for_each_venue!(frames_for!(out, rng, seq;));
    }
    out
}

/// One frame per rejection reason that `dispatch` can reach, plus one unknown venue.
fn bad_frames(good: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let truncated = good[0][..HEADER_LEN + 2].to_vec();
    let mut bad_magic = good[1].clone();
    bad_magic[0] ^= 0xFF;
    let mut bad_version = good[2].clone();
    bad_version[3] = 9;
    let mut bad_length = good[3].clone();
    bad_length[4] = bad_length[4].wrapping_add(1);
    let mut bad_checksum = good[4].clone();
    let last = bad_checksum.len() - 1;
    bad_checksum[last] ^= 0x01;
    let mut unknown_venue = good[5].clone();
    unknown_venue[2] = 200;
    vec![truncated, bad_magic, bad_version, bad_length, bad_checksum, unknown_venue]
}

#[derive(Default)]
struct Totals {
    digest: u64,
    records: u64,
    instructions: u64,
    cycles: u64,
    counted: bool,
}

/// Runs every good frame through one sink type: one warm-up round, then `ROUNDS` measured rounds.
fn measure<S: Sink + Default>(good: &[Vec<u8>], totals: &mut Totals) {
    let mut sink = S::default();
    let mut stats = Stats::default();
    for frame in good {
        dispatch(frame, &mut sink, &mut stats).expect("warm-up frame rejected");
    }
    let before = counters::snapshot();
    let mut records = 0u64;
    for _ in 0..ROUNDS {
        for frame in good {
            records += dispatch(frame, &mut sink, &mut stats).expect("good frame rejected") as u64;
        }
    }
    let after = counters::snapshot();
    if let (Some(b), Some(a)) = (before, after) {
        totals.instructions += a.instructions - b.instructions;
        totals.cycles += a.cycles - b.cycles;
        totals.counted = true;
    }
    totals.records += records;
    totals.digest = totals.digest.rotate_left(9) ^ sink.digest() ^ stats.digest();
}

/// One pass over all four sink types: 48 `(decoder, sink)` pairs.
fn measure_all(good: &[Vec<u8>]) -> Totals {
    let mut totals = Totals::default();
    measure::<Checksum>(good, &mut totals);
    measure::<Vwap>(good, &mut totals);
    measure::<LastPx>(good, &mut totals);
    measure::<Range>(good, &mut totals);
    totals
}

fn main() {
    let good = good_frames();
    let bad = bad_frames(&good);

    // Interrupts taken while the loop runs are charged to the process, so the counter only
    // ever reads high. Keep the lowest of a few passes, which is the least disturbed one.
    let mut totals = measure_all(&good);
    for _ in 1..PASSES {
        let again = measure_all(&good);
        assert_eq!(again.digest, totals.digest, "a pass computed a different digest");
        if again.instructions < totals.instructions {
            totals = again;
        }
    }

    // The rejection paths, outside the measured region: every version must reject the same
    // frames for the same reason and build a message of the same length.
    let mut sink = Checksum::default();
    let mut stats = Stats::default();
    let mut reject_digest = 0u64;
    for frame in &bad {
        match dispatch(frame, &mut sink, &mut stats) {
            Ok(_) => panic!("corrupt frame accepted"),
            Err(e) => reject_digest = reject_digest.rotate_left(8) ^ (e.code() << 16) ^ e.context_len(),
        }
    }
    // A decoder handed another venue's frame, which `dispatch` can never do.
    let wrong = kit::ingest(&good[1], &Nyse, &mut sink, &mut stats).expect_err("wrong venue accepted");
    reject_digest = reject_digest.rotate_left(8) ^ (wrong.code() << 16) ^ wrong.context_len();

    let per_record = if totals.counted {
        // Whole instructions: the count repeats to about 0.2 percent, which a decimal would expose.
        format!("{:.0}", totals.instructions as f64 / totals.records as f64)
    } else {
        "unavailable".to_string()
    };
    println!(
        "digest={:016x} rejects={} reject_digest={:016x} records={} insn_per_record={}",
        totals.digest, stats.rejects, reject_digest, totals.records, per_record
    );
    if totals.counted {
        eprintln!(
            "unrecorded: {} instructions, {} cycles, {:.2} instructions per cycle",
            totals.instructions,
            totals.cycles,
            totals.instructions as f64 / totals.cycles as f64
        );
    }
}
