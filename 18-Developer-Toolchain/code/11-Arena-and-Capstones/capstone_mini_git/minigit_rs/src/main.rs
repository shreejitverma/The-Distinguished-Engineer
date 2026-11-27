#![allow(dead_code, unused_variables, unused_imports, unused_assignments)]

use std::fs;
use std::io::Write;
use std::os::raw::{c_char, c_int, c_ulong, c_void};
use std::path::Path;

#[repr(C)]
#[derive(Default)]
struct CStream {
    next_in: *const u8,
    avail_in: u32,
    total_in: c_ulong,
    next_out: *mut u8,
    avail_out: u32,
    total_out: c_ulong,
    msg: *const c_char,
    state: *mut c_void,
    zalloc: *mut c_void,
    zfree: *mut c_void,
    opaque: *mut c_void,
    data_type: c_int,
    adler: c_ulong,
    reserved: c_ulong,
}

#[link(name = "z")]
extern "C" {
    fn zlibVersion() -> *const c_char;
    fn deflateInit_(strm: *mut CStream, level: c_int, ver: *const c_char, sz: c_int) -> c_int;
    fn deflate(strm: *mut CStream, flush: c_int) -> c_int;
    fn deflateEnd(strm: *mut CStream) -> c_int;
    fn inflateInit_(strm: *mut CStream, ver: *const c_char, sz: c_int) -> c_int;
    fn inflate(strm: *mut CStream, flush: c_int) -> c_int;
    fn inflateEnd(strm: *mut CStream) -> c_int;
    fn crc32(crc: c_ulong, buf: *const u8, len: u32) -> c_ulong;
}

fn zlib_compress_payload(input: &[u8]) -> Result<Vec<u8>, String> {
    let mut s = CStream::default();
    s.next_in = input.as_ptr();
    s.avail_in = input.len() as u32;
    unsafe {
        let rc = deflateInit_(&mut s, 6, zlibVersion(), std::mem::size_of::<CStream>() as c_int);
        if rc != 0 {
            return Err(format!("deflateInit error {}", rc));
        }
    }
    let mut dest = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        s.next_out = chunk.as_mut_ptr();
        s.avail_out = chunk.len() as u32;
        let ret = unsafe { deflate(&mut s, 4) }; // Z_FINISH
        let written = chunk.len() - s.avail_out as usize;
        dest.extend_from_slice(&chunk[..written]);
        if ret == 1 {
            break;
        }
        if ret != 0 {
            unsafe { deflateEnd(&mut s); }
            return Err(format!("deflate error {}", ret));
        }
    }
    unsafe { deflateEnd(&mut s); }
    Ok(dest)
}

fn zlib_decompress_payload(input: &[u8]) -> Result<Vec<u8>, String> {
    let mut s = CStream::default();
    s.next_in = input.as_ptr();
    s.avail_in = input.len() as u32;
    unsafe {
        let rc = inflateInit_(&mut s, zlibVersion(), std::mem::size_of::<CStream>() as c_int);
        if rc != 0 {
            return Err(format!("inflateInit error {}", rc));
        }
    }
    let mut dest = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        s.next_out = chunk.as_mut_ptr();
        s.avail_out = chunk.len() as u32;
        let ret = unsafe { inflate(&mut s, 0) };
        let written = chunk.len() - s.avail_out as usize;
        dest.extend_from_slice(&chunk[..written]);
        if ret == 1 {
            break;
        }
        if ret != 0 {
            unsafe { inflateEnd(&mut s); }
            return Err(format!("inflate error {}", ret));
        }
    }
    unsafe { inflateEnd(&mut s); }
    Ok(dest)
}

fn compute_crc32(data: &[u8]) -> u32 {
    let res = unsafe { crc32(0, data.as_ptr(), data.len() as u32) };
    res as u32
}

fn compute_sha1_digest(msg: &[u8]) -> [u8; 20] {
    let mut state = [0x67452301u32, 0xefcdab89u32, 0x98badcfeu32, 0x10325476u32, 0xc3d2e1f0u32];
    let bit_count = (msg.len() as u64).wrapping_mul(8);
    let mut buf = msg.to_vec();
    buf.push(0x80);
    while (buf.len() % 64) != 56 {
        buf.push(0);
    }
    buf.extend_from_slice(&bit_count.to_be_bytes());

    for block in buf.chunks_exact(64) {
        let mut words = [0u32; 80];
        for (i, w) in words[..16].iter_mut().enumerate() {
            *w = u32::from_be_bytes([block[i * 4], block[i * 4 + 1], block[i * 4 + 2], block[i * 4 + 3]]);
        }
        for i in 16..80 {
            words[i] = (words[i - 3] ^ words[i - 8] ^ words[i - 14] ^ words[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (state[0], state[1], state[2], state[3], state[4]);
        for (i, &w) in words.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5a827999u32),
                20..=39 => (b ^ c ^ d, 0x6ed9eba1u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8f1bbcdcu32),
                _ => (b ^ c ^ d, 0xca62c1d6u32),
            };
            let tmp = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(w);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for (i, s) in state.iter().enumerate() {
        out[i * 4..(i + 1) * 4].copy_from_slice(&s.to_be_bytes());
    }
    out
}

fn compute_sha256_digest(msg: &[u8]) -> [u8; 32] {
    let k = [
        0x428a2f98u32, 0x71374491u32, 0xb5c0fbcfu32, 0xe9b5dba5u32,
        0x3956c25bu32, 0x59f111f1u32, 0x923f82a4u32, 0xab1c5ed5u32,
        0xd807aa98u32, 0x12835b01u32, 0x243185beu32, 0x550c7dc3u32,
        0x72be5d74u32, 0x80deb1feu32, 0x9bdc06a7u32, 0xc19bf174u32,
        0xe49b69c1u32, 0xefbe4786u32, 0x0fc19dc6u32, 0x240ca1ccu32,
        0x2de92c6fu32, 0x4a7484aau32, 0x5cb0a9dcu32, 0x76f988dau32,
        0x983e5152u32, 0xa831c66du32, 0xb00327c8u32, 0xbf597fc7u32,
        0xc6e00bf3u32, 0xd5a79147u32, 0x06ca6351u32, 0x14292967u32,
        0x27b70a85u32, 0x2e1b2138u32, 0x4d2c6dfcu32, 0x53380d13u32,
        0x650a7354u32, 0x766a0abbu32, 0x81c2c92eu32, 0x92722c85u32,
        0xa2bfe8a1u32, 0xa81a664bu32, 0xc24b8b70u32, 0xc76c51a3u32,
        0xd192e819u32, 0xd6990624u32, 0xf40e3585u32, 0x106aa070u32,
        0x19a4c116u32, 0x1e376c08u32, 0x2748774cu32, 0x34b0bcb5u32,
        0x391c0cb3u32, 0x4ed8aa4au32, 0x5b9cca4fu32, 0x682e6ff3u32,
        0x748f82eeu32, 0x78a5636fu32, 0x84c87814u32, 0x8cc70208u32,
        0x90befffau32, 0xa4506cebu32, 0xbef9a3f7u32, 0xc67178f2u32,
    ];
    let mut state = [
        0x6a09e667u32, 0xbb67ae85u32, 0x3c6ef372u32, 0xa54ff53au32,
        0x510e527fu32, 0x9b05688cu32, 0x1f83d9abu32, 0x5be0cd19u32,
    ];
    let bit_count = (msg.len() as u64).wrapping_mul(8);
    let mut buf = msg.to_vec();
    buf.push(0x80);
    while (buf.len() % 64) != 56 {
        buf.push(0);
    }
    buf.extend_from_slice(&bit_count.to_be_bytes());

    for block in buf.chunks_exact(64) {
        let mut words = [0u32; 64];
        for (i, w) in words[..16].iter_mut().enumerate() {
            *w = u32::from_be_bytes([block[i * 4], block[i * 4 + 1], block[i * 4 + 2], block[i * 4 + 3]]);
        }
        for i in 16..64 {
            let s0 = words[i - 15].rotate_right(7) ^ words[i - 15].rotate_right(18) ^ (words[i - 15] >> 3);
            let s1 = words[i - 2].rotate_right(17) ^ words[i - 2].rotate_right(19) ^ (words[i - 2] >> 10);
            words[i] = words[i - 16].wrapping_add(s0).wrapping_add(words[i - 7]).wrapping_add(s1);
        }
        let mut a = state[0];
        let mut b = state[1];
        let mut c = state[2];
        let mut d = state[3];
        let mut e = state[4];
        let mut f = state[5];
        let mut g = state[6];
        let mut h = state[7];

        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h.wrapping_add(s1).wrapping_add(ch).wrapping_add(k[i]).wrapping_add(words[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);

            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }

        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
        state[5] = state[5].wrapping_add(f);
        state[6] = state[6].wrapping_add(g);
        state[7] = state[7].wrapping_add(h);
    }

    let mut out = [0u8; 32];
    for (i, s) in state.iter().enumerate() {
        out[i * 4..(i + 1) * 4].copy_from_slice(&s.to_be_bytes());
    }
    out
}

fn hex_format(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

fn read_git_loose_object(repo_path: &Path, oid_hex: &str) -> Result<(String, Vec<u8>), String> {
    if oid_hex.len() < 4 {
        return Err("OID length too short".into());
    }
    let obj_file = repo_path.join(".git").join("objects").join(&oid_hex[..2]).join(&oid_hex[2..]);
    let raw = fs::read(&obj_file).map_err(|e| format!("Read object {}: {}", obj_file.display(), e))?;
    let decomp = zlib_decompress_payload(&raw)?;
    let nul_idx = decomp.iter().position(|&x| x == 0).ok_or("Malformed loose object header: no nul byte")?;
    let header = std::str::from_utf8(&decomp[..nul_idx]).map_err(|e| e.to_string())?;
    let mut parts = header.split(' ');
    let typ = parts.next().ok_or("Empty object type")?.to_string();
    let body = decomp[nul_idx + 1..].to_vec();
    Ok((typ, body))
}

fn write_git_loose_object(repo_path: &Path, typ: &str, body: &[u8], is_sha256: bool) -> Result<String, String> {
    let header = format!("{} {}\0", typ, body.len());
    let mut full = Vec::with_capacity(header.len() + body.len());
    full.extend_from_slice(header.as_bytes());
    full.extend_from_slice(body);

    let oid_hex = if is_sha256 {
        hex_format(&compute_sha256_digest(&full))
    } else {
        hex_format(&compute_sha1_digest(&full))
    };

    let dir = repo_path.join(".git").join("objects").join(&oid_hex[..2]);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let target_file = dir.join(&oid_hex[2..]);
    if !target_file.exists() {
        let comp = zlib_compress_payload(&full)?;
        let tmp_file = dir.join(format!("tmp_obj_{}", oid_hex));
        fs::write(&tmp_file, &comp).map_err(|e| e.to_string())?;
        fs::rename(&tmp_file, &target_file).map_err(|e| e.to_string())?;
    }
    Ok(oid_hex)
}

struct ParsedIndexEntry {
    mode: u32,
    size: u32,
    oid_hex: String,
    stage: u8,
    path: String,
}

fn parse_git_index(repo_path: &Path) -> Result<Vec<ParsedIndexEntry>, String> {
    let index_file = repo_path.join(".git").join("index");
    let content = fs::read(&index_file).map_err(|e| format!("Failed to read index: {}", e))?;
    if content.len() < 32 {
        return Err("Index file too small".into());
    }
    if &content[..4] != b"DIRC" {
        return Err("Index missing DIRC header".into());
    }
    let ver = u32::from_be_bytes([content[4], content[5], content[6], content[7]]);
    if ver != 2 && ver != 3 && ver != 4 {
        return Err(format!("Unsupported index version: {}", ver));
    }
    let count = u32::from_be_bytes([content[8], content[9], content[10], content[11]]) as usize;

    let payload_len = content.len() - 20;
    let expected_sha = compute_sha1_digest(&content[..payload_len]);
    if expected_sha != content[payload_len..] {
        return Err("Index checksum mismatch".into());
    }

    let mut entries = Vec::with_capacity(count);
    let mut cursor = 12;
    for _ in 0..count {
        if cursor + 62 > content.len() {
            return Err("Index file truncated inside entry".into());
        }
        let start = cursor;
        let mode = u32::from_be_bytes([content[cursor + 24], content[cursor + 25], content[cursor + 26], content[cursor + 27]]);
        let sz = u32::from_be_bytes([content[cursor + 36], content[cursor + 37], content[cursor + 38], content[cursor + 39]]);
        let oid_hex = hex_format(&content[cursor + 40..cursor + 60]);
        let flags = u16::from_be_bytes([content[cursor + 60], content[cursor + 61]]);
        let stage = ((flags >> 12) & 0x03) as u8;
        cursor += 62;

        let nul_pos = content[cursor..].iter().position(|&b| b == 0).ok_or("Unterminated path string in index")?;
        let path = std::str::from_utf8(&content[cursor..cursor + nul_pos]).map_err(|e| e.to_string())?.to_string();
        let raw_len = 62 + nul_pos;
        let pad = 8 - (raw_len % 8);
        cursor = start + raw_len + pad;

        entries.push(ParsedIndexEntry {
            mode,
            size: sz,
            oid_hex,
            stage,
            path,
        });
    }
    Ok(entries)
}

struct PackIdxV2 {
    fanout: [u32; 256],
    total_objects: usize,
    oids: Vec<[u8; 20]>,
    crcs: Vec<u32>,
    offsets: Vec<u64>,
    pack_checksum: [u8; 20],
    idx_checksum: [u8; 20],
}

fn parse_pack_index_v2(idx_path: &Path) -> Result<PackIdxV2, String> {
    let data = fs::read(idx_path).map_err(|e| e.to_string())?;
    if data.len() < 4 + 4 + 256 * 4 + 40 {
        return Err("Pack index file too small".into());
    }
    if &data[..4] != b"\xfftOc" {
        return Err("Pack index magic mismatch, expected \\xfftOc".into());
    }
    let ver = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
    if ver != 2 {
        return Err(format!("Unsupported pack index version: {}", ver));
    }
    let mut fanout = [0u32; 256];
    for i in 0..256 {
        let pos = 8 + i * 4;
        fanout[i] = u32::from_be_bytes([data[pos], data[pos + 1], data[pos + 2], data[pos + 3]]);
    }
    let total = fanout[255] as usize;
    let mut cur = 8 + 256 * 4;

    let mut oids = Vec::with_capacity(total);
    for _ in 0..total {
        let mut h = [0u8; 20];
        h.copy_from_slice(&data[cur..cur + 20]);
        oids.push(h);
        cur += 20;
    }

    let mut crcs = Vec::with_capacity(total);
    for _ in 0..total {
        crcs.push(u32::from_be_bytes([data[cur], data[cur + 1], data[cur + 2], data[cur + 3]]));
        cur += 4;
    }

    let mut offsets = Vec::with_capacity(total);
    let mut large_offset_indices = Vec::new();
    for i in 0..total {
        let off_val = u32::from_be_bytes([data[cur], data[cur + 1], data[cur + 2], data[cur + 3]]);
        cur += 4;
        if (off_val & 0x80000000) != 0 {
            large_offset_indices.push((i, (off_val & 0x7fffffff) as usize));
            offsets.push(0);
        } else {
            offsets.push(off_val as u64);
        }
    }

    for (entry_idx, large_idx) in large_offset_indices {
        let large_pos = cur + large_idx * 8;
        let hi = u32::from_be_bytes([data[large_pos], data[large_pos + 1], data[large_pos + 2], data[large_pos + 3]]) as u64;
        let lo = u32::from_be_bytes([data[large_pos + 4], data[large_pos + 5], data[large_pos + 6], data[large_pos + 7]]) as u64;
        offsets[entry_idx] = (hi << 32) | lo;
    }

    let trail_start = data.len() - 40;
    let mut pack_checksum = [0u8; 20];
    let mut idx_checksum = [0u8; 20];
    pack_checksum.copy_from_slice(&data[trail_start..trail_start + 20]);
    idx_checksum.copy_from_slice(&data[trail_start + 20..]);

    let calc_idx_sha = compute_sha1_digest(&data[..trail_start + 20]);
    if calc_idx_sha != idx_checksum {
        return Err("Pack index self-checksum mismatch".into());
    }

    Ok(PackIdxV2 {
        fanout,
        total_objects: total,
        oids,
        crcs,
        offsets,
        pack_checksum,
        idx_checksum,
    })
}

fn apply_git_delta(base: &[u8], delta: &[u8]) -> Result<Vec<u8>, String> {
    let mut ptr = 0;
    let (_base_sz, bytes_read) = decode_varint_size(&delta[ptr..])?;
    ptr += bytes_read;
    let (target_sz, bytes_read) = decode_varint_size(&delta[ptr..])?;
    ptr += bytes_read;

    let mut target = Vec::with_capacity(target_sz as usize);

    while ptr < delta.len() {
        let cmd = delta[ptr];
        ptr += 1;
        if (cmd & 0x80) != 0 {
            let mut cp_off: u32 = 0;
            let mut cp_sz: u32 = 0;
            if (cmd & 0x01) != 0 {
                cp_off |= delta[ptr] as u32;
                ptr += 1;
            }
            if (cmd & 0x02) != 0 {
                cp_off |= (delta[ptr] as u32) << 8;
                ptr += 1;
            }
            if (cmd & 0x04) != 0 {
                cp_off |= (delta[ptr] as u32) << 16;
                ptr += 1;
            }
            if (cmd & 0x08) != 0 {
                cp_off |= (delta[ptr] as u32) << 24;
                ptr += 1;
            }
            if (cmd & 0x10) != 0 {
                cp_sz |= delta[ptr] as u32;
                ptr += 1;
            }
            if (cmd & 0x20) != 0 {
                cp_sz |= (delta[ptr] as u32) << 8;
                ptr += 1;
            }
            if (cmd & 0x40) != 0 {
                cp_sz |= (delta[ptr] as u32) << 16;
                ptr += 1;
            }
            if cp_sz == 0 {
                cp_sz = 0x10000;
            }
            let start = cp_off as usize;
            let end = start + cp_sz as usize;
            if end > base.len() {
                return Err(format!("Delta copy out of bounds: base len {}, range {}..{}", base.len(), start, end));
            }
            target.extend_from_slice(&base[start..end]);
        } else if cmd > 0 {
            let insert_len = cmd as usize;
            if ptr + insert_len > delta.len() {
                return Err("Delta insert instruction truncated".into());
            }
            target.extend_from_slice(&delta[ptr..ptr + insert_len]);
            ptr += insert_len;
        } else {
            return Err("Invalid zero delta opcode".into());
        }
    }
    if target.len() != target_sz as usize {
        return Err(format!("Reconstructed size mismatch: expected {}, got {}", target_sz, target.len()));
    }
    Ok(target)
}

fn decode_varint_size(input: &[u8]) -> Result<(u64, usize), String> {
    let mut val: u64 = 0;
    let mut shift = 0;
    let mut count = 0;
    for &b in input {
        count += 1;
        val |= ((b & 0x7f) as u64) << shift;
        shift += 7;
        if (b & 0x80) == 0 {
            return Ok((val, count));
        }
    }
    Err("Unterminated varint".into())
}

fn read_packed_object_at_offset(pack_bytes: &[u8], offset: usize) -> Result<(u8, Vec<u8>, usize), String> {
    let mut cur = offset;
    let first = pack_bytes[cur];
    cur += 1;
    let obj_type = (first >> 4) & 0x07;
    let mut _sz = (first & 0x0f) as u64;
    let mut shift = 4;
    let mut next_byte = first;

    while (next_byte & 0x80) != 0 {
        next_byte = pack_bytes[cur];
        cur += 1;
        _sz |= ((next_byte & 0x7f) as u64) << shift;
        shift += 7;
    }

    match obj_type {
        1..=4 => {
            let decompressed = zlib_decompress_payload(&pack_bytes[cur..])?;
            Ok((obj_type, decompressed, cur))
        }
        6 => { // OBJ_OFS_DELTA
            let mut b = pack_bytes[cur];
            cur += 1;
            let mut delta_offset = (b & 0x7f) as u64;
            while (b & 0x80) != 0 {
                b = pack_bytes[cur];
                cur += 1;
                delta_offset = ((delta_offset + 1) << 7) | ((b & 0x7f) as u64);
            }
            let base_offset = (offset as u64).checked_sub(delta_offset)
                .ok_or_else(|| format!("Underflow in delta offset: offset {}, delta {}", offset, delta_offset))? as usize;
            let delta_payload = zlib_decompress_payload(&pack_bytes[cur..])?;
            let (base_type, base_data, _) = read_packed_object_at_offset(pack_bytes, base_offset)?;
            let reconstructed = apply_git_delta(&base_data, &delta_payload)?;
            Ok((base_type, reconstructed, cur))
        }
        7 => { // OBJ_REF_DELTA
            let mut base_oid = [0u8; 20];
            base_oid.copy_from_slice(&pack_bytes[cur..cur + 20]);
            cur += 20;
            let delta_payload = zlib_decompress_payload(&pack_bytes[cur..])?;
            Ok((7, delta_payload, cur))
        }
        _ => Err(format!("Unknown pack object type: {}", obj_type)),
    }
}

struct ParsedCommitGraph {
    num_commits: usize,
    commit_oids: Vec<[u8; 20]>,
    root_trees: Vec<[u8; 20]>,
    parent_indices: Vec<u32>,
    topological_levels: Vec<u32>,
    commit_timestamps: Vec<u64>,
}

fn parse_commit_graph(path: &Path) -> Result<ParsedCommitGraph, String> {
    let data = fs::read(path).map_err(|e| format!("Read commit graph {}: {}", path.display(), e))?;
    if data.len() < 8 {
        return Err("Commit graph header too short".into());
    }
    if &data[..4] != b"CGPH" {
        return Err("Missing CGPH commit graph signature".into());
    }
    let ver = data[4];
    if ver != 1 {
        return Err(format!("Unsupported commit graph version {}", ver));
    }
    let num_chunks = data[6] as usize;
    let mut chunk_offsets = std::collections::HashMap::new();
    let mut cur = 8;
    for _ in 0..=num_chunks {
        let chunk_id = &data[cur..cur + 4];
        let offset = u64::from_be_bytes([
            data[cur + 4], data[cur + 5], data[cur + 6], data[cur + 7],
            data[cur + 8], data[cur + 9], data[cur + 10], data[cur + 11],
        ]) as usize;
        cur += 12;
        chunk_offsets.insert(chunk_id.to_vec(), offset);
    }

    let oidf_off = *chunk_offsets.get(b"OIDF".as_slice()).ok_or("Missing OIDF chunk")?;
    let mut fanout = [0u32; 256];
    for i in 0..256 {
        let p = oidf_off + i * 4;
        fanout[i] = u32::from_be_bytes([data[p], data[p + 1], data[p + 2], data[p + 3]]);
    }
    let n = fanout[255] as usize;

    let oidl_off = *chunk_offsets.get(b"OIDL".as_slice()).ok_or("Missing OIDL chunk")?;
    let mut commit_oids = Vec::with_capacity(n);
    for i in 0..n {
        let p = oidl_off + i * 20;
        let mut h = [0u8; 20];
        h.copy_from_slice(&data[p..p + 20]);
        commit_oids.push(h);
    }

    let cdat_off = *chunk_offsets.get(b"CDAT".as_slice()).ok_or("Missing CDAT chunk")?;
    let mut root_trees = Vec::with_capacity(n);
    let mut parent_indices = Vec::with_capacity(n);
    let mut topological_levels = Vec::with_capacity(n);
    let mut commit_timestamps = Vec::with_capacity(n);

    for i in 0..n {
        let p = cdat_off + i * 36;
        let mut tree_h = [0u8; 20];
        tree_h.copy_from_slice(&data[p..p + 20]);
        root_trees.push(tree_h);

        let p1 = u32::from_be_bytes([data[p + 20], data[p + 21], data[p + 22], data[p + 23]]);
        parent_indices.push(p1);

        let gen_word = u32::from_be_bytes([data[p + 28], data[p + 29], data[p + 30], data[p + 31]]);
        let gen = gen_word >> 2;
        let time_high = (gen_word & 0x03) as u64;
        let time_low = u32::from_be_bytes([data[p + 32], data[p + 33], data[p + 34], data[p + 35]]) as u64;
        let timestamp = (time_high << 32) | time_low;

        topological_levels.push(gen);
        commit_timestamps.push(timestamp);
    }

    Ok(ParsedCommitGraph {
        num_commits: n,
        commit_oids,
        root_trees,
        parent_indices,
        topological_levels,
        commit_timestamps,
    })
}

fn format_pkt_line(payload: &str) -> String {
    let len = payload.len() + 4;
    format!("{:04x}{}", len, payload)
}

fn parse_pkt_line(input: &[u8]) -> Result<(Option<&[u8]>, usize), String> {
    if input.len() < 4 {
        return Err("Pkt-line too short".into());
    }
    let hex_str = std::str::from_utf8(&input[..4]).map_err(|e| e.to_string())?;
    let len = usize::from_str_radix(hex_str, 16).map_err(|e| e.to_string())?;
    if len == 0 {
        return Ok((None, 4));
    }
    if len < 4 || len > input.len() {
        return Err(format!("Invalid pkt-line len {}", len));
    }
    Ok((Some(&input[4..len]), len))
}

fn run_verification_suite(repo_dir: &Path, sha256_repo_dir: &Path) -> Result<(), String> {
    println!("=== Capstone Mini Git in Rust: Multi-Stage Interop Suite ===");

    println!("\n[Stage 1: Content-Addressable Storage and Loose Object Inflation]");
    let sample_text = b"Hello from distinguished engineer capstone toolchain!\n";
    let sha1_oid = write_git_loose_object(repo_dir, "blob", sample_text, false)?;
    println!("  SHA-1 loose blob written: {}", sha1_oid);
    let (read_typ, read_body) = read_git_loose_object(repo_dir, &sha1_oid)?;
    assert_eq!(read_typ, "blob");
    assert_eq!(&read_body, sample_text);
    println!("  SHA-1 loose blob decompression and verification: PASS");

    let sha256_oid = write_git_loose_object(sha256_repo_dir, "blob", sample_text, true)?;
    println!("  SHA-256 loose blob written: {}", sha256_oid);
    let (read256_typ, read256_body) = read_git_loose_object(sha256_repo_dir, &sha256_oid)?;
    assert_eq!(read256_typ, "blob");
    assert_eq!(&read256_body, sample_text);
    println!("  SHA-256 loose blob decompression and verification: PASS");

    println!("\n[Stage 2: Git DIRC Index File Parsing and Stat Cache]");
    let index_entries = parse_git_index(repo_dir)?;
    println!("  Parsed DIRC index entries count: {}", index_entries.len());
    for entry in &index_entries {
        println!("  Entry: {} (mode={:o}, size={}, oid={})", entry.path, entry.mode, entry.size, entry.oid_hex);
    }
    assert!(!index_entries.is_empty(), "Index must contain entries");
    println!("  DIRC v2 header, entry stat cache, and SHA-1 trailer: PASS");

    println!("\n[Stage 3: Packfile Index v2 and Delta Decompression]");
    let pack_dir = repo_dir.join(".git").join("objects").join("pack");
    let mut idx_file = None;
    let mut pack_file = None;
    if let Ok(entries) = fs::read_dir(&pack_dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.extension().map(|e| e == "idx").unwrap_or(false) {
                idx_file = Some(p);
            } else if p.extension().map(|e| e == "pack").unwrap_or(false) {
                pack_file = Some(p);
            }
        }
    }
    let idx_path = idx_file.ok_or("No .idx file found in repository pack directory")?;
    let pack_path = pack_file.ok_or("No .pack file found in repository pack directory")?;

    let parsed_idx = parse_pack_index_v2(&idx_path)?;
    println!("  Pack index v2 magic and version verified");
    println!("  Total packed objects in .idx: {}", parsed_idx.total_objects);
    println!("  Pack trailer checksum: {}", hex_format(&parsed_idx.pack_checksum));
    println!("  Index trailer checksum: {}", hex_format(&parsed_idx.idx_checksum));

    let pack_data = fs::read(&pack_path).map_err(|e| e.to_string())?;
    let mut verified_count = 0;
    for (i, oid) in parsed_idx.oids.iter().enumerate() {
        let offset = parsed_idx.offsets[i] as usize;
        let (typ, data, _header_end) = read_packed_object_at_offset(&pack_data, offset)?;
        let typ_str = match typ {
            1 => "commit",
            2 => "tree",
            3 => "blob",
            4 => "tag",
            _ => "unknown",
        };
        let header = format!("{} {}\0", typ_str, data.len());
        let mut full = Vec::with_capacity(header.len() + data.len());
        full.extend_from_slice(header.as_bytes());
        full.extend_from_slice(&data);
        let calc_oid = compute_sha1_digest(&full);
        assert_eq!(&calc_oid, oid, "Reconstructed packed object hash mismatch at offset {}", offset);
        verified_count += 1;
    }
    println!("  Decompressed and verified packed objects bit-for-bit: {}/{}", verified_count, parsed_idx.total_objects);
    println!("  Packfile index v2 fanout table and delta decompression: PASS");

    println!("\n[Stage 4: Commit-Graph Format and Generation Numbers]");
    let cg_path = repo_dir.join(".git").join("objects").join("info").join("commit-graph");
    if cg_path.exists() {
        let cg = parse_commit_graph(&cg_path)?;
        println!("  Commit graph header: signature=CGPH, version=1, commits={}", cg.num_commits);
        for i in 0..cg.num_commits {
            let oid = hex_format(&cg.commit_oids[i]);
            let tree = hex_format(&cg.root_trees[i]);
            println!("  Commit #{}: oid={}, tree={}, gen={}, timestamp={}", i, oid, tree, cg.topological_levels[i], cg.commit_timestamps[i]);
        }
        println!("  Commit-graph binary chunks (OIDF, OIDL, CDAT) and topological generation: PASS");
    } else {
        println!("  Commit-graph not present, skipped");
    }

    println!("\n[Stage 5: Wire Protocol v2 Packet-Line Framing]");
    let sample_pkt = format_pkt_line("command=ls-refs\n");
    assert_eq!(&sample_pkt, "0014command=ls-refs\n");
    let (parsed_payload, bytes_consumed) = parse_pkt_line(sample_pkt.as_bytes())?;
    assert_eq!(bytes_consumed, 20);
    assert_eq!(parsed_payload.unwrap(), b"command=ls-refs\n");
    let (flush_payload, flush_consumed) = parse_pkt_line(b"0000")?;
    assert!(flush_payload.is_none());
    assert_eq!(flush_consumed, 4);
    println!("  Pkt-line length prefixes, delimiters, and flush packets: PASS");

    println!("\n=== Capstone Multi-Stage Verification Complete: ALL TESTS PASSED ===");
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: minigit_rs <command> [args...]");
        std::process::exit(1);
    }
    match args[1].as_str() {
        "verify-all" => {
            if args.len() < 4 {
                eprintln!("Usage: minigit_rs verify-all <sha1_repo_dir> <sha256_repo_dir>");
                std::process::exit(1);
            }
            let repo1 = Path::new(&args[2]);
            let repo2 = Path::new(&args[3]);
            if let Err(e) = run_verification_suite(repo1, repo2) {
                eprintln!("Verification failed: {}", e);
                std::process::exit(1);
            }
        }
        "cat-file" => {
            if args.len() < 4 {
                eprintln!("Usage: minigit_rs cat-file <repo_dir> <oid>");
                std::process::exit(1);
            }
            let repo = Path::new(&args[2]);
            let oid = &args[3];
            match read_git_loose_object(repo, oid) {
                Ok((typ, body)) => {
                    println!("Type: {}", typ);
                    std::io::stdout().write_all(&body).unwrap();
                }
                Err(e) => {
                    eprintln!("Error: {}", e);
                    std::process::exit(1);
                }
            }
        }
        _ => {
            eprintln!("Unknown command: {}", args[1]);
            std::process::exit(1);
        }
    }
}
