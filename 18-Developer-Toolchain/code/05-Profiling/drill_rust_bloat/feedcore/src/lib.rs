//! Shared wire types for the drill: ticks, venue decoders, sinks, frames, and counters.
//!
//! Nothing here changes between the three library versions, so every difference the lab
//! prints comes from how `ingest` is written in `kit_bloated`, `kit_dyn`, and `kit_lean`.

pub mod counters;

/// One normalized market-data update.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tick {
    pub instrument: u32,
    pub px: i64,
    pub qty: u32,
}

/// A venue-specific record layout. The associated constants make it usable only through
/// static dispatch, which is what the generic `ingest` versions rely on.
pub trait Decoder {
    const VENUE: u8;
    const RECORD_LEN: usize;
    fn decode(&self, rec: &[u8]) -> Tick;
}

/// The same contract in a dyn-compatible shape, for `kit_dyn`.
pub trait DynDecoder {
    fn venue(&self) -> u8;
    fn record_len(&self) -> usize;
    fn decode_dyn(&self, rec: &[u8]) -> Tick;
}

impl<D: Decoder> DynDecoder for D {
    fn venue(&self) -> u8 {
        D::VENUE
    }
    fn record_len(&self) -> usize {
        D::RECORD_LEN
    }
    fn decode_dyn(&self, rec: &[u8]) -> Tick {
        self.decode(rec)
    }
}

/// A consumer of ticks. Dyn-compatible, and tiny on purpose: it is the per-record hot kernel.
pub trait Sink {
    fn push(&mut self, tick: Tick);
    fn digest(&self) -> u64;
}

#[inline]
fn u32le(rec: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([rec[off], rec[off + 1], rec[off + 2], rec[off + 3]])
}

/// Declares one venue: id, record length, the three field offsets, and the price scale.
/// Every venue differs in at least one constant, so no two `decode` bodies are identical
/// and LLVM's MergeFunctions pass cannot fold the copies back together.
macro_rules! venue {
    ($name:ident, $id:expr, $len:expr, $inst:expr, $px:expr, $qty:expr, $scale:expr) => {
        #[derive(Clone, Copy, Debug, Default)]
        pub struct $name;

        impl Decoder for $name {
            const VENUE: u8 = $id;
            const RECORD_LEN: usize = $len;

            #[inline]
            fn decode(&self, rec: &[u8]) -> Tick {
                Tick {
                    instrument: u32le(rec, $inst),
                    px: i64::from(u32le(rec, $px)) * $scale,
                    qty: u32le(rec, $qty),
                }
            }
        }
    };
}

venue!(Nyse, 1, 12, 0, 4, 8, 100);
venue!(Nasdaq, 2, 12, 4, 0, 8, 100);
venue!(Arca, 3, 12, 8, 4, 0, 100);
venue!(Bats, 4, 12, 0, 8, 4, 10);
venue!(Iex, 5, 16, 0, 4, 8, 1);
venue!(Cme, 6, 16, 4, 8, 12, 25);
venue!(Ice, 7, 16, 8, 12, 0, 5);
venue!(Eurex, 8, 16, 12, 0, 4, 50);
venue!(Lse, 9, 16, 0, 8, 12, 1000);
venue!(Tse, 10, 16, 4, 12, 0, 2);
venue!(Hkex, 11, 12, 4, 8, 0, 20);
venue!(Asx, 12, 12, 8, 0, 4, 4);

/// Calls a macro with the full venue list, so the driver and the tests cannot drift from it.
#[macro_export]
macro_rules! for_each_venue {
    ($callback:ident ! ( $($prefix:tt)* )) => {
        $callback!($($prefix)* Nyse, Nasdaq, Arca, Bats, Iex, Cme, Ice, Eurex, Lse, Tse, Hkex, Asx)
    };
}

/// Order-sensitive rolling checksum of every field.
#[derive(Default)]
pub struct Checksum(u64);

impl Sink for Checksum {
    #[inline]
    fn push(&mut self, t: Tick) {
        self.0 = self.0.rotate_left(5)
            ^ u64::from(t.instrument)
            ^ (t.px as u64)
            ^ (u64::from(t.qty) << 32);
    }
    fn digest(&self) -> u64 {
        self.0
    }
}

/// Notional and volume, the two sums a VWAP needs.
#[derive(Default)]
pub struct Vwap {
    notional: u64,
    volume: u64,
}

impl Sink for Vwap {
    #[inline]
    fn push(&mut self, t: Tick) {
        self.notional = self
            .notional
            .wrapping_add((t.px as u64).wrapping_mul(u64::from(t.qty)));
        self.volume = self.volume.wrapping_add(u64::from(t.qty));
    }
    fn digest(&self) -> u64 {
        self.notional ^ self.volume.rotate_left(17)
    }
}

/// Last price per instrument slot.
pub struct LastPx([i64; 64]);

impl Default for LastPx {
    fn default() -> Self {
        LastPx([0; 64])
    }
}

impl Sink for LastPx {
    #[inline]
    fn push(&mut self, t: Tick) {
        self.0[(t.instrument & 63) as usize] = t.px;
    }
    fn digest(&self) -> u64 {
        self.0
            .iter()
            .fold(0u64, |acc, px| acc.rotate_left(7) ^ (*px as u64))
    }
}

/// Price range seen so far.
pub struct Range {
    lo: i64,
    hi: i64,
}

impl Default for Range {
    fn default() -> Self {
        Range {
            lo: i64::MAX,
            hi: i64::MIN,
        }
    }
}

impl Sink for Range {
    #[inline]
    fn push(&mut self, t: Tick) {
        self.lo = self.lo.min(t.px);
        self.hi = self.hi.max(t.px);
    }
    fn digest(&self) -> u64 {
        (self.lo as u64).rotate_left(32) ^ (self.hi as u64)
    }
}

pub const MAGIC: u16 = 0xFEED;
pub const VERSION: u8 = 2;
pub const HEADER_LEN: usize = 12;
pub const TRAILER_LEN: usize = 4;
pub const MAX_RECORDS: usize = 512;

/// Per-feed accounting that every `ingest` version updates the same way.
pub struct Stats {
    pub frames: u64,
    pub records: u64,
    pub bytes: u64,
    pub rejects: u64,
    pub gaps: u64,
    pub last_seq: [u32; 256],
    pub batch_hist: [u64; 8],
}

impl Default for Stats {
    fn default() -> Self {
        Stats {
            frames: 0,
            records: 0,
            bytes: 0,
            rejects: 0,
            gaps: 0,
            last_seq: [0; 256],
            batch_hist: [0; 8],
        }
    }
}

impl Stats {
    /// Folds every counter into one value, so two library versions can be compared exactly.
    pub fn digest(&self) -> u64 {
        let mut d = self.frames ^ self.records.rotate_left(13) ^ self.bytes.rotate_left(29);
        d ^= self.rejects.rotate_left(41) ^ self.gaps.rotate_left(53);
        for s in self.last_seq {
            d = d.rotate_left(3) ^ u64::from(s);
        }
        for h in self.batch_hist {
            d = d.rotate_left(11) ^ h;
        }
        d
    }
}

/// Why a frame was rejected. The `String` context is the cold-path weight in this drill:
/// building it pulls `core::fmt` machinery into whatever function constructs the error.
#[derive(Debug)]
pub enum IngestError {
    Truncated(String),
    BadMagic(String),
    BadVersion(String),
    WrongVenue(String),
    BadLength(String),
    BadChecksum(String),
    UnknownVenue(u8),
}

impl IngestError {
    /// Stable small integer per variant, used by the driver's reject accounting.
    pub fn code(&self) -> u64 {
        match self {
            IngestError::Truncated(_) => 1,
            IngestError::BadMagic(_) => 2,
            IngestError::BadVersion(_) => 3,
            IngestError::WrongVenue(_) => 4,
            IngestError::BadLength(_) => 5,
            IngestError::BadChecksum(_) => 6,
            IngestError::UnknownVenue(_) => 7,
        }
    }

    pub fn context_len(&self) -> u64 {
        match self {
            IngestError::Truncated(s)
            | IngestError::BadMagic(s)
            | IngestError::BadVersion(s)
            | IngestError::WrongVenue(s)
            | IngestError::BadLength(s)
            | IngestError::BadChecksum(s) => s.len() as u64,
            IngestError::UnknownVenue(_) => 0,
        }
    }
}

/// Sum of the payload's little-endian 32-bit words. Record lengths are multiples of four.
#[inline]
pub fn payload_sum(payload: &[u8]) -> u32 {
    let mut sum = 0u32;
    for w in payload.as_chunks::<4>().0 {
        sum = sum.wrapping_add(u32::from_le_bytes(*w));
    }
    sum
}

/// Builds one valid frame: 12-byte header, `count` records, 4-byte trailer checksum.
pub fn build_frame(venue: u8, record_len: usize, seq: u32, count: usize, rng: &mut Lcg) -> Vec<u8> {
    let payload_len = count * record_len;
    let mut f = Vec::with_capacity(HEADER_LEN + payload_len + TRAILER_LEN);
    f.extend_from_slice(&MAGIC.to_le_bytes());
    f.push(venue);
    f.push(VERSION);
    f.extend_from_slice(&(count as u16).to_le_bytes());
    f.extend_from_slice(&(payload_len as u16).to_le_bytes());
    f.extend_from_slice(&seq.to_le_bytes());
    for _ in 0..payload_len / 4 {
        // Keep every field under 2^20 so prices and quantities stay small and positive.
        f.extend_from_slice(&(rng.next() & 0x000F_FFFF).to_le_bytes());
    }
    let sum = payload_sum(&f[HEADER_LEN..]);
    f.extend_from_slice(&sum.to_le_bytes());
    f
}

/// Deterministic generator, so every run and every platform sees the same frames.
pub struct Lcg(pub u64);

impl Lcg {
    #[allow(clippy::should_implement_trait)]
    pub fn next(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) as u32
    }
}
