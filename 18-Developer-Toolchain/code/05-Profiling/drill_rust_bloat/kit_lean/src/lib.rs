//! Version 3 of the library: the fix.
//!
//! The work is split by what it depends on, not by where it sits in the call:
//!
//! - `open_frame` and `close_frame` depend on the frame bytes and two integers, so they are
//!   ordinary non-generic functions. They are compiled once, in this crate, however many
//!   decoder and sink types the application uses.
//! - Rejecting a frame is rare, so the error construction sits behind `#[cold]` and
//!   `#[inline(never)]` and never shares a page with the path that accepts a frame.
//! - `ingest` keeps its generic signature, so callers do not change, but its body is three
//!   lines: open, run the per-record kernel, close. Only the kernel needs `D` and `S`, and
//!   it has to stay generic, because that is what lets `decode` and `push` inline into it.

use feedcore::{
    payload_sum, Decoder, IngestError, Sink, Stats, HEADER_LEN, MAGIC, MAX_RECORDS, TRAILER_LEN,
    VERSION,
};

fn hex_head(frame: &[u8]) -> String {
    let mut s = String::with_capacity(16);
    for b in frame.iter().take(8) {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

fn field16(frame: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([frame[off], frame[off + 1]])
}

fn field32(frame: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([frame[off], frame[off + 1], frame[off + 2], frame[off + 3]])
}

/// What went wrong, as plain data. Building the message is deferred to `reject`.
enum Fault {
    Truncated,
    BadMagic(u16),
    BadVersion,
    WrongVenue,
    BadLength {
        seq: u32,
        count: usize,
        payload_len: usize,
    },
    BadChecksum {
        seq: u32,
        declared: u32,
        actual: u32,
    },
}

#[cold]
#[inline(never)]
fn reject(
    fault: Fault,
    frame: &[u8],
    venue: u8,
    record_len: usize,
    stats: &mut Stats,
) -> IngestError {
    stats.rejects += 1;
    let head = hex_head(frame);
    match fault {
        Fault::Truncated => IngestError::Truncated(format!(
            "venue {} frame of {} bytes is shorter than header plus trailer ({}); head={}",
            venue,
            frame.len(),
            HEADER_LEN + TRAILER_LEN,
            head
        )),
        Fault::BadMagic(magic) => IngestError::BadMagic(format!(
            "venue {} expected magic {:#06x}, got {:#06x}; head={}",
            venue, MAGIC, magic, head
        )),
        Fault::BadVersion => IngestError::BadVersion(format!(
            "venue {} expected version {}, got {}; head={}",
            venue, VERSION, frame[3], head
        )),
        Fault::WrongVenue => IngestError::WrongVenue(format!(
            "decoder for venue {} was handed a frame for venue {}; head={}",
            venue, frame[2], head
        )),
        Fault::BadLength { seq, count, payload_len } => IngestError::BadLength(format!(
            "venue {} seq {} declares {} records of {} bytes in {} payload bytes, frame is {} bytes; head={}",
            venue,
            seq,
            count,
            record_len,
            payload_len,
            frame.len(),
            head
        )),
        Fault::BadChecksum { seq, declared, actual } => IngestError::BadChecksum(format!(
            "venue {} seq {} checksum {:#010x} does not match payload sum {:#010x}; head={}",
            venue, seq, declared, actual, head
        )),
    }
}

/// Validates the frame and returns its payload.
/// Non-generic: one copy for every caller.
pub fn open_frame<'a>(
    frame: &'a [u8],
    venue: u8,
    record_len: usize,
    stats: &mut Stats,
) -> Result<&'a [u8], IngestError> {
    if frame.len() < HEADER_LEN + TRAILER_LEN {
        return Err(reject(Fault::Truncated, frame, venue, record_len, stats));
    }
    let magic = field16(frame, 0);
    if magic != MAGIC {
        return Err(reject(
            Fault::BadMagic(magic),
            frame,
            venue,
            record_len,
            stats,
        ));
    }
    if frame[3] != VERSION {
        return Err(reject(Fault::BadVersion, frame, venue, record_len, stats));
    }
    if frame[2] != venue {
        return Err(reject(Fault::WrongVenue, frame, venue, record_len, stats));
    }
    let count = field16(frame, 4) as usize;
    let payload_len = field16(frame, 6) as usize;
    let seq = field32(frame, 8);
    if count > MAX_RECORDS
        || payload_len != count * record_len
        || frame.len() != HEADER_LEN + payload_len + TRAILER_LEN
    {
        return Err(reject(
            Fault::BadLength {
                seq,
                count,
                payload_len,
            },
            frame,
            venue,
            record_len,
            stats,
        ));
    }
    let payload = &frame[HEADER_LEN..HEADER_LEN + payload_len];
    let declared = field32(frame, HEADER_LEN + payload_len);
    let actual = payload_sum(payload);
    if declared != actual {
        return Err(reject(
            Fault::BadChecksum {
                seq,
                declared,
                actual,
            },
            frame,
            venue,
            record_len,
            stats,
        ));
    }
    let slot = &mut stats.last_seq[venue as usize];
    if *slot != 0 && seq != slot.wrapping_add(1) {
        stats.gaps += 1;
    }
    *slot = seq;
    Ok(payload)
}

/// Accounts for one accepted frame.
/// Non-generic: one copy for every caller.
pub fn close_frame(stats: &mut Stats, count: usize, frame_len: usize) {
    stats.frames += 1;
    stats.records += count as u64;
    stats.bytes += frame_len as u64;
    let bucket = (usize::BITS - count.leading_zeros()).min(7) as usize;
    stats.batch_hist[bucket] += 1;
}

/// Validates one frame, decodes its records with `dec`, and feeds them to `sink`.
///
/// Same signature as the bloated version. `#[inline]` is deliberate: the shim is three
/// lines, and inlining it puts the kernel loop directly in the caller's dispatch arm.
#[inline]
pub fn ingest<D: Decoder, S: Sink>(
    frame: &[u8],
    dec: &D,
    sink: &mut S,
    stats: &mut Stats,
) -> Result<usize, IngestError> {
    let payload = open_frame(frame, D::VENUE, D::RECORD_LEN, stats)?;
    let mut count = 0;
    for rec in payload.chunks_exact(D::RECORD_LEN) {
        sink.push(dec.decode(rec));
        count += 1;
    }
    close_frame(stats, count, frame.len());
    Ok(count)
}
