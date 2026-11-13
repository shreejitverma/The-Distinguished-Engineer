//! Version 2 of the library: the tempting fix that trades one regression for another.
//!
//! `ingest` takes trait objects, so it is compiled exactly once and the build is fast again.
//! The price is paid per record: `decode_dyn` and `push` are now indirect calls through a
//! vtable, so neither can be inlined into the loop, the record length is no longer a
//! constant the optimizer can see, and every field read keeps its bounds check.

use feedcore::{
    payload_sum, DynDecoder, IngestError, Sink, Stats, HEADER_LEN, MAGIC, MAX_RECORDS, TRAILER_LEN,
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

/// Validates one frame, decodes its records with `dec`, and feeds them to `sink`.
pub fn ingest(
    frame: &[u8],
    dec: &dyn DynDecoder,
    sink: &mut dyn Sink,
    stats: &mut Stats,
) -> Result<usize, IngestError> {
    let venue = dec.venue();
    let record_len = dec.record_len();
    if frame.len() < HEADER_LEN + TRAILER_LEN {
        stats.rejects += 1;
        return Err(IngestError::Truncated(format!(
            "venue {} frame of {} bytes is shorter than header plus trailer ({}); head={}",
            venue,
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
            venue,
            MAGIC,
            magic,
            hex_head(frame)
        )));
    }
    if frame[3] != VERSION {
        stats.rejects += 1;
        return Err(IngestError::BadVersion(format!(
            "venue {} expected version {}, got {}; head={}",
            venue,
            VERSION,
            frame[3],
            hex_head(frame)
        )));
    }
    if frame[2] != venue {
        stats.rejects += 1;
        return Err(IngestError::WrongVenue(format!(
            "decoder for venue {} was handed a frame for venue {}; head={}",
            venue,
            frame[2],
            hex_head(frame)
        )));
    }
    let count = field16(frame, 4) as usize;
    let payload_len = field16(frame, 6) as usize;
    let seq = field32(frame, 8);
    if count > MAX_RECORDS
        || payload_len != count * record_len
        || frame.len() != HEADER_LEN + payload_len + TRAILER_LEN
    {
        stats.rejects += 1;
        return Err(IngestError::BadLength(format!(
            "venue {} seq {} declares {} records of {} bytes in {} payload bytes, frame is {} bytes; head={}",
            venue,
            seq,
            count,
            record_len,
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
            venue,
            seq,
            declared,
            actual,
            hex_head(frame)
        )));
    }
    let slot = &mut stats.last_seq[venue as usize];
    if *slot != 0 && seq != slot.wrapping_add(1) {
        stats.gaps += 1;
    }
    *slot = seq;
    for rec in payload.chunks_exact(record_len) {
        sink.push(dec.decode_dyn(rec));
    }
    stats.frames += 1;
    stats.records += count as u64;
    stats.bytes += frame.len() as u64;
    let bucket = (usize::BITS - count.leading_zeros()).min(7) as usize;
    stats.batch_hist[bucket] += 1;
    Ok(count)
}
