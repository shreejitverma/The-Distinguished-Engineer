//! Version 1 of the library: the regression.
//!
//! Everything `ingest` does is written inside one function that is generic over the decoder
//! and the sink, and the helpers are generic too "because they need `D::VENUE`" and marked
//! `#[inline(always)]` "because ingest is hot". Each `(D, S)` pair the application uses
//! therefore gets its own copy of the header checks, the checksum loop, the sequence
//! tracking, and all of the error formatting, although none of that depends on `D` or `S`
//! beyond two integers.

use feedcore::{
    payload_sum, Decoder, IngestError, Sink, Stats, HEADER_LEN, MAGIC, MAX_RECORDS, TRAILER_LEN,
    VERSION,
};

// The `plain_helpers` feature removes this one attribute and nothing else, so the lab can
// show how much of the machine code is forced inlining rather than monomorphization.
#[cfg_attr(not(feature = "plain_helpers"), inline(always))]
fn hex_head(frame: &[u8]) -> String {
    let mut s = String::with_capacity(16);
    for b in frame.iter().take(8) {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

#[inline(always)]
fn field16(frame: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([frame[off], frame[off + 1]])
}

#[inline(always)]
fn field32(frame: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([frame[off], frame[off + 1], frame[off + 2], frame[off + 3]])
}

#[inline(always)]
fn track_sequence<D: Decoder>(stats: &mut Stats, seq: u32) {
    let slot = &mut stats.last_seq[D::VENUE as usize];
    if *slot != 0 && seq != slot.wrapping_add(1) {
        stats.gaps += 1;
    }
    *slot = seq;
}

#[inline(always)]
fn account(stats: &mut Stats, count: usize, frame_len: usize) {
    stats.frames += 1;
    stats.records += count as u64;
    stats.bytes += frame_len as u64;
    let bucket = (usize::BITS - count.leading_zeros()).min(7) as usize;
    stats.batch_hist[bucket] += 1;
}

/// Validates one frame, decodes its records with `dec`, and feeds them to `sink`.
pub fn ingest<D: Decoder, S: Sink>(
    frame: &[u8],
    dec: &D,
    sink: &mut S,
    stats: &mut Stats,
) -> Result<usize, IngestError> {
    if frame.len() < HEADER_LEN + TRAILER_LEN {
        stats.rejects += 1;
        return Err(IngestError::Truncated(format!(
            "venue {} frame of {} bytes is shorter than header plus trailer ({}); head={}",
            D::VENUE,
            frame.len(),
            HEADER_LEN + TRAILER_LEN,
            hex_head(frame)
        )));
    }
    let magic = field16(frame, 0);
    if magic != MAGIC {
        stats.rejects += 1;
        return Err(IngestError::BadMagic(format!(
            "venue {} expected magic {:#06x}, got {:#06x}; head={}",
            D::VENUE,
            MAGIC,
            magic,
            hex_head(frame)
        )));
    }
    if frame[3] != VERSION {
        stats.rejects += 1;
        return Err(IngestError::BadVersion(format!(
            "venue {} expected version {}, got {}; head={}",
            D::VENUE,
            VERSION,
            frame[3],
            hex_head(frame)
        )));
    }
    if frame[2] != D::VENUE {
        stats.rejects += 1;
        return Err(IngestError::WrongVenue(format!(
            "decoder for venue {} was handed a frame for venue {}; head={}",
            D::VENUE,
            frame[2],
            hex_head(frame)
        )));
    }
    let count = field16(frame, 4) as usize;
    let payload_len = field16(frame, 6) as usize;
    let seq = field32(frame, 8);
    if count > MAX_RECORDS
        || payload_len != count * D::RECORD_LEN
        || frame.len() != HEADER_LEN + payload_len + TRAILER_LEN
    {
        stats.rejects += 1;
        return Err(IngestError::BadLength(format!(
            "venue {} seq {} declares {} records of {} bytes in {} payload bytes, frame is {} bytes; head={}",
            D::VENUE,
            seq,
            count,
            D::RECORD_LEN,
            payload_len,
            frame.len(),
            hex_head(frame)
        )));
    }
    let payload = &frame[HEADER_LEN..HEADER_LEN + payload_len];
    let declared = field32(frame, HEADER_LEN + payload_len);
    let actual = payload_sum(payload);
    if declared != actual {
        stats.rejects += 1;
        return Err(IngestError::BadChecksum(format!(
            "venue {} seq {} checksum {:#010x} does not match payload sum {:#010x}; head={}",
            D::VENUE,
            seq,
            declared,
            actual,
            hex_head(frame)
        )));
    }
    track_sequence::<D>(stats, seq);
    for rec in payload.chunks_exact(D::RECORD_LEN) {
        sink.push(dec.decode(rec));
    }
    account(stats, count, frame.len());
    Ok(count)
}
