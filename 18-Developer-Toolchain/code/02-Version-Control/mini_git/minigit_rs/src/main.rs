use std::fs;
use std::os::raw::{c_char, c_int, c_ulong, c_void};
use std::path::Path;

#[repr(C)]
#[derive(Default)]
struct ZStream {
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
    fn inflateInit_(strm: *mut ZStream, version: *const c_char, stream_size: c_int) -> c_int;
    fn inflate(strm: *mut ZStream, flush: c_int) -> c_int;
    fn inflateEnd(strm: *mut ZStream) -> c_int;
    fn crc32(crc: c_ulong, buf: *const u8, len: u32) -> c_ulong;
}

fn zlib_decompress(compressed: &[u8]) -> Result<Vec<u8>, String> {
    let mut strm = ZStream::default();
    strm.next_in = compressed.as_ptr();
    strm.avail_in = compressed.len() as u32;

    unsafe {
        let ret = inflateInit_(&mut strm, zlibVersion(), std::mem::size_of::<ZStream>() as c_int);
        if ret != 0 {
            return Err(format!("inflateInit failed: {}", ret));
        }
    }

    let mut out = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        strm.next_out = buf.as_mut_ptr();
        strm.avail_out = buf.len() as u32;
        let ret = unsafe { inflate(&mut strm, 0) };
        let produced = buf.len() - strm.avail_out as usize;
        out.extend_from_slice(&buf[..produced]);
        if ret == 1 {
            break;
        }
        if ret != 0 {
            unsafe { inflateEnd(&mut strm); }
            return Err(format!("inflate error: {}", ret));
        }
    }
    unsafe { inflateEnd(&mut strm); }
    Ok(out)
}

fn calc_crc32(data: &[u8]) -> u32 {
    let crc = unsafe { crc32(0, data.as_ptr(), data.len() as u32) };
    crc as u32
}

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h = [0x67452301u32, 0xEFCDAB89u32, 0x98BADCFEu32, 0x10325476u32, 0xC3D2E1F0u32];
    let bit_len = (data.len() as u64) * 8;
    let mut msg = data.to_vec();
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[i * 4], chunk[i * 4 + 1], chunk[i * 4 + 2], chunk[i * 4 + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for i in 0..80 {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDCu32),
                _ => (b ^ c ^ d, 0xCA62C1D6u32),
            };
            let temp = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(w[i]);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for i in 0..5 {
        out[i * 4..(i + 1) * 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

fn from_hex(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 {
        return Err("Odd hex length".to_string());
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

fn read_loose_object(repo: &Path, oid_hex: &str) -> Result<(String, Vec<u8>), String> {
    if oid_hex.len() < 4 {
        return Err("Invalid OID".to_string());
    }
    let p = repo.join(".git").join("objects").join(&oid_hex[0..2]).join(&oid_hex[2..]);
    let compressed = fs::read(&p).map_err(|e| format!("Failed to read {}: {}", p.display(), e))?;
    let raw = zlib_decompress(&compressed)?;
    let nul_pos = raw.iter().position(|&b| b == 0).ok_or("Missing null byte in header")?;
    let header_str = std::str::from_utf8(&raw[..nul_pos]).map_err(|e| e.to_string())?;
    let parts: Vec<&str> = header_str.split(' ').collect();
    if parts.len() != 2 {
        return Err(format!("Malformed header: {}", header_str));
    }
    let obj_type = parts[0].to_string();
    let body = raw[nul_pos + 1..].to_vec();
    let declared: usize = parts[1].parse().map_err(|e| format!("Bad size in header: {}", e))?;
    if declared != body.len() {
        return Err(format!("Loose object {} size mismatch: {} != {}", oid_hex, declared, body.len()));
    }
    let actual = to_hex(&sha1(&raw));
    if actual != oid_hex {
        return Err(format!("Loose object hash mismatch: {} != {}", actual, oid_hex));
    }
    Ok((obj_type, body))
}

#[allow(dead_code)]
#[derive(Debug)]
struct IndexEntry {
    mode: u32,
    size: u32,
    sha1: [u8; 20],
    path: String,
}

fn parse_index(repo: &Path) -> Result<Vec<IndexEntry>, String> {
    let index_path = repo.join(".git").join("index");
    let data = fs::read(&index_path).map_err(|e| format!("Failed to read index: {}", e))?;
    if data.len() < 32 {
        return Err("Index file too small".to_string());
    }
    if &data[0..4] != b"DIRC" {
        return Err("Invalid magic: expected DIRC".to_string());
    }
    let version = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
    if version != 2 {
        return Err(format!("Expected index version 2, got {}", version));
    }
    let count = u32::from_be_bytes([data[8], data[9], data[10], data[11]]) as usize;

    let payload_len = data.len() - 20;
    let expected_trailer = sha1(&data[..payload_len]);
    if expected_trailer != data[payload_len..] {
        return Err("Index SHA-1 trailer checksum mismatch".to_string());
    }

    let mut entries = Vec::with_capacity(count);
    let mut pos = 12;
    for _ in 0..count {
        let entry_start = pos;
        let mode = u32::from_be_bytes([data[pos + 24], data[pos + 25], data[pos + 26], data[pos + 27]]);
        let size = u32::from_be_bytes([data[pos + 36], data[pos + 37], data[pos + 38], data[pos + 39]]);
        let mut sha1_bytes = [0u8; 20];
        sha1_bytes.copy_from_slice(&data[pos + 40..pos + 60]);
        pos += 62;
        let nul_idx = data[pos..].iter().position(|&b| b == 0).ok_or("Unterminated path in index entry")?;
        let path_str = std::str::from_utf8(&data[pos..pos + nul_idx]).map_err(|e| e.to_string())?.to_string();
        let raw_len = 62 + nul_idx;
        let pad_len = 8 - (raw_len % 8);
        pos = entry_start + raw_len + pad_len;
        entries.push(IndexEntry {
            mode,
            size,
            sha1: sha1_bytes,
            path: path_str,
        });
    }
    if pos != payload_len {
        return Err(format!("Index entries end at {} but the trailer starts at {}", pos, payload_len));
    }
    Ok(entries)
}

#[derive(Debug)]
struct TreeEntry {
    mode: u32,
    oid_hex: String,
    name: String,
}

fn parse_tree(repo: &Path, tree_oid: &str) -> Result<Vec<TreeEntry>, String> {
    let (obj_type, data) = read_loose_object(repo, tree_oid)?;
    if obj_type != "tree" {
        return Err(format!("Expected tree, got {}", obj_type));
    }
    let mut entries = Vec::new();
    let mut pos = 0;
    while pos < data.len() {
        let space_pos = data[pos..].iter().position(|&b| b == b' ').ok_or("Missing space in tree entry")? + pos;
        let mode_str = std::str::from_utf8(&data[pos..space_pos]).map_err(|e| e.to_string())?;
        let mode = u32::from_str_radix(mode_str, 8).map_err(|e| e.to_string())?;
        let nul_pos = data[space_pos..].iter().position(|&b| b == 0).ok_or("Missing NUL in tree entry")? + space_pos;
        let name = std::str::from_utf8(&data[space_pos + 1..nul_pos]).map_err(|e| e.to_string())?.to_string();
        let oid_bytes = &data[nul_pos + 1..nul_pos + 21];
        let oid_hex = to_hex(oid_bytes);
        pos = nul_pos + 21;
        entries.push(TreeEntry { mode, oid_hex, name });
    }
    Ok(entries)
}

fn apply_delta(base: &[u8], delta: &[u8]) -> Result<Vec<u8>, String> {
    let mut pos = 0;
    let mut read_varint = || -> Result<usize, String> {
        let mut res = 0usize;
        let mut shift = 0;
        loop {
            if pos >= delta.len() {
                return Err("Truncated varint in delta".to_string());
            }
            let b = delta[pos];
            pos += 1;
            res |= ((b & 0x7F) as usize) << shift;
            shift += 7;
            if (b & 0x80) == 0 {
                break;
            }
        }
        Ok(res)
    };

    let base_sz = read_varint()?;
    let target_sz = read_varint()?;
    if base_sz != base.len() {
        return Err(format!("Base size mismatch: {} != {}", base_sz, base.len()));
    }

    let mut out = Vec::with_capacity(target_sz);
    while pos < delta.len() {
        let op = delta[pos];
        pos += 1;
        if (op & 0x80) != 0 {
            // Copy instruction
            let mut off = 0usize;
            if (op & 0x01) != 0 { off |= delta[pos] as usize; pos += 1; }
            if (op & 0x02) != 0 { off |= (delta[pos] as usize) << 8; pos += 1; }
            if (op & 0x04) != 0 { off |= (delta[pos] as usize) << 16; pos += 1; }
            if (op & 0x08) != 0 { off |= (delta[pos] as usize) << 24; pos += 1; }
            let mut sz = 0usize;
            if (op & 0x10) != 0 { sz |= delta[pos] as usize; pos += 1; }
            if (op & 0x20) != 0 { sz |= (delta[pos] as usize) << 8; pos += 1; }
            if (op & 0x40) != 0 { sz |= (delta[pos] as usize) << 16; pos += 1; }
            if sz == 0 { sz = 0x10000; }
            if off + sz > base.len() {
                return Err(format!("Copy bounds exceed base: {} + {} > {}", off, sz, base.len()));
            }
            out.extend_from_slice(&base[off..off + sz]);
        } else if op > 0 {
            // Insert literal
            let take = op as usize;
            if pos + take > delta.len() {
                return Err("Insert bounds exceed delta".to_string());
            }
            out.extend_from_slice(&delta[pos..pos + take]);
            pos += take;
        } else {
            return Err("Invalid zero delta opcode".to_string());
        }
    }
    if out.len() != target_sz {
        return Err(format!("Reconstructed size mismatch: {} != {}", out.len(), target_sz));
    }
    Ok(out)
}

fn decode_ofs_delta_offset(pack_data: &[u8], mut pos: usize) -> (usize, usize) {
    let mut c = pack_data[pos];
    pos += 1;
    let mut offset = (c & 0x7F) as usize;
    while (c & 0x80) != 0 {
        c = pack_data[pos];
        pos += 1;
        offset = ((offset + 1) << 7) | ((c & 0x7F) as usize);
    }
    (offset, pos)
}

/// Lists every object ID stored in a pack index v2, in index (sorted) order.
fn list_idx_oids(idx_path: &Path) -> Result<Vec<String>, String> {
    let idx_data = fs::read(idx_path).map_err(|e| format!("Failed to read .idx: {}", e))?;
    if idx_data.len() < 8 + 1024 + 40 {
        return Err("Index file too small".to_string());
    }
    let off = 8 + 255 * 4;
    let total = u32::from_be_bytes([idx_data[off], idx_data[off + 1], idx_data[off + 2], idx_data[off + 3]]) as usize;
    let names_offset = 8 + 1024;
    Ok((0..total).map(|i| to_hex(&idx_data[names_offset + i * 20..names_offset + (i + 1) * 20])).collect())
}

/// Reads one object from a pack. Returns (type name, payload, whether the entry is a delta).
fn read_pack_object(pack_path: &Path, idx_path: &Path, target_oid_hex: &str) -> Result<(String, Vec<u8>, bool), String> {
    let idx_data = fs::read(idx_path).map_err(|e| format!("Failed to read .idx: {}", e))?;
    let pack_data = fs::read(pack_path).map_err(|e| format!("Failed to read .pack: {}", e))?;

    if idx_data.len() < 8 + 1024 + 40 {
        return Err("Index file too small".to_string());
    }
    if &idx_data[0..4] != b"\xfftOc" {
        return Err("Invalid idx signature".to_string());
    }

    let fanout_slice = &idx_data[8..8 + 1024];
    let mut fanout = [0u32; 256];
    for i in 0..256 {
        fanout[i] = u32::from_be_bytes([
            fanout_slice[i * 4],
            fanout_slice[i * 4 + 1],
            fanout_slice[i * 4 + 2],
            fanout_slice[i * 4 + 3],
        ]);
    }
    let total_objects = fanout[255] as usize;
    let names_offset = 8 + 1024;
    let crcs_offset = names_offset + total_objects * 20;
    let offsets_offset = crcs_offset + total_objects * 4;

    let target_raw = from_hex(target_oid_hex)?;
    let first_byte = target_raw[0] as usize;
    let mut lo = if first_byte > 0 { fanout[first_byte - 1] as usize } else { 0 };
    let mut hi = fanout[first_byte] as usize;

    let mut obj_idx = None;
    while lo < hi {
        let mid = (lo + hi) / 2;
        let cand = &idx_data[names_offset + mid * 20..names_offset + (mid + 1) * 20];
        if cand < &target_raw[..] {
            lo = mid + 1;
        } else if cand > &target_raw[..] {
            hi = mid;
        } else {
            obj_idx = Some(mid);
            break;
        }
    }
    let idx = obj_idx.ok_or_else(|| format!("Object {} not found in idx", target_oid_hex))?;
    let pack_offset = u32::from_be_bytes([
        idx_data[offsets_offset + idx * 4],
        idx_data[offsets_offset + idx * 4 + 1],
        idx_data[offsets_offset + idx * 4 + 2],
        idx_data[offsets_offset + idx * 4 + 3],
    ]) as usize;

    let stored_crc = u32::from_be_bytes([
        idx_data[crcs_offset + idx * 4],
        idx_data[crcs_offset + idx * 4 + 1],
        idx_data[crcs_offset + idx * 4 + 2],
        idx_data[crcs_offset + idx * 4 + 3],
    ]);

    fn parse_entry_at(data: &[u8], offset: usize) -> Result<(u8, Vec<u8>), String> {
        let mut pos = offset;
        let mut b = data[pos];
        pos += 1;
        let type_id = (b >> 4) & 7;
        let mut expected_sz = (b & 0x0F) as usize;
        let mut shift = 4;
        while (b & 0x80) != 0 {
            b = data[pos];
            pos += 1;
            expected_sz |= ((b & 0x7F) as usize) << shift;
            shift += 7;
        }

        if type_id == 6 {
            // OBJ_OFS_DELTA
            let (dist, new_pos) = decode_ofs_delta_offset(data, pos);
            let base_offset = offset - dist;
            let delta_bytes = zlib_decompress(&data[new_pos..])?;
            if delta_bytes.len() != expected_sz {
                return Err(format!("Delta size mismatch: {} != {}", delta_bytes.len(), expected_sz));
            }
            let (base_type, base_payload) = parse_entry_at(data, base_offset)?;
            let reconstructed = apply_delta(&base_payload, &delta_bytes)?;
            Ok((base_type, reconstructed))
        } else if type_id >= 1 && type_id <= 4 {
            let decompressed = zlib_decompress(&data[pos..])?;
            if decompressed.len() != expected_sz {
                return Err(format!("Object size mismatch: {} != {}", decompressed.len(), expected_sz));
            }
            Ok((type_id, decompressed))
        } else {
            Err(format!("Unsupported packed type: {}", type_id))
        }
    }

    // The CRC32 in the .idx covers the packed entry bytes: from its offset to the next entry
    // (or to the pack trailer for the last one).
    let pack_trailer_start = pack_data.len() - 20;
    let entry_end = (0..total_objects)
        .map(|i| {
            let o = offsets_offset + i * 4;
            u32::from_be_bytes([idx_data[o], idx_data[o + 1], idx_data[o + 2], idx_data[o + 3]]) as usize
        })
        .filter(|&o| o > pack_offset)
        .min()
        .unwrap_or(pack_trailer_start);
    let actual_crc = calc_crc32(&pack_data[pack_offset..entry_end]);
    if actual_crc != stored_crc {
        return Err(format!("CRC32 mismatch for {}: {:08x} != {:08x}", target_oid_hex, actual_crc, stored_crc));
    }

    let (type_id, payload) = parse_entry_at(&pack_data, pack_offset)?;
    let is_delta = ((pack_data[pack_offset] >> 4) & 7) == 6;
    let type_name = match type_id {
        1 => "commit",
        2 => "tree",
        3 => "blob",
        4 => "tag",
        _ => return Err(format!("Unknown type {}", type_id)),
    };

    let mut prefix = format!("{} {}\0", type_name, payload.len()).into_bytes();
    prefix.extend_from_slice(&payload);
    let calc_sha = to_hex(&sha1(&prefix));
    if calc_sha != target_oid_hex {
        return Err(format!("Reconstructed SHA mismatch: {} != {}", calc_sha, target_oid_hex));
    }
    Ok((type_name.to_string(), payload, is_delta))
}

fn checkout_tree_recursive(repo: &Path, tree_oid: &str, target_dir: &Path) -> Result<usize, String> {
    use std::os::unix::fs::PermissionsExt;
    let entries = parse_tree(repo, tree_oid)?;
    let mut count = 0;
    for entry in entries {
        let dest = target_dir.join(&entry.name);
        if entry.mode == 0o040000 {
            fs::create_dir_all(&dest).map_err(|e| e.to_string())?;
            count += checkout_tree_recursive(repo, &entry.oid_hex, &dest)?;
        } else {
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let (_, blob) = read_loose_object(repo, &entry.oid_hex)?;
            fs::write(&dest, blob).map_err(|e| e.to_string())?;
            fs::set_permissions(&dest, fs::Permissions::from_mode(entry.mode & 0o777)).map_err(|e| e.to_string())?;
            count += 1;
        }
    }
    Ok(count)
}

/// Git's tree order: a directory name sorts as if it ended with '/'.
fn tree_sort_key(e: &TreeEntry) -> Vec<u8> {
    let mut k = e.name.clone().into_bytes();
    if e.mode == 0o040000 {
        k.push(b'/');
    }
    k
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: minigit_rs verify-all <repo> <pack_path> <idx_path> <blob_oid> <tree_oid> <checkout_dir>");
        std::process::exit(1);
    }

    if args[1] == "verify-all" {
        use std::os::unix::fs::PermissionsExt;
        let repo = Path::new(&args[2]);
        let pack_path = Path::new(&args[3]);
        let idx_path = Path::new(&args[4]);
        let blob_oid = &args[5];
        let tree_oid = &args[6];
        let checkout_dir = Path::new(&args[7]);

        // 1. Cat-file loose blob (read_loose_object also re-hashes the object)
        let (b_type, b_data) = read_loose_object(repo, blob_oid).expect("Failed loose blob read");
        assert_eq!(b_type, "blob");
        assert_eq!(b_data, b"hello world\n");
        println!("Rust cat-file verified loose blob: PASS");

        // 2. Index parser: entries must consume the file exactly up to the 20-byte trailer
        let idx_entries = parse_index(repo).expect("Failed index parse");
        let idx_view: Vec<(&str, u32, u32)> = idx_entries.iter().map(|e| (e.path.as_str(), e.mode, e.size)).collect();
        assert_eq!(
            idx_view,
            vec![
                ("build.sh", 0o100755, 27),
                ("hello.txt", 0o100644, 12),
                ("src.txt", 0o100644, 39),
                ("src/main.c", 0o100644, 25)
            ]
        );
        println!("Rust index parser verified DIRC v2 alignment and trailer: PASS");

        // 3. Tree parser: slash-sorted, and entries agree with the index
        let tree_entries = parse_tree(repo, tree_oid).expect("Failed tree parse");
        let keys: Vec<Vec<u8>> = tree_entries.iter().map(tree_sort_key).collect();
        assert!(keys.windows(2).all(|w| w[0] < w[1]), "tree entries are not in base_name_compare order");
        let tree_view: Vec<(&str, u32)> = tree_entries.iter().map(|e| (e.name.as_str(), e.mode)).collect();
        assert_eq!(
            tree_view,
            vec![("build.sh", 0o100755), ("hello.txt", 0o100644), ("src.txt", 0o100644), ("src", 0o040000)]
        );
        assert_eq!(tree_entries[0].oid_hex, to_hex(&idx_entries[0].sha1));
        assert_eq!(tree_entries[1].oid_hex, to_hex(&idx_entries[1].sha1));
        assert_eq!(tree_entries[2].oid_hex, to_hex(&idx_entries[2].sha1));
        let sub_entries = parse_tree(repo, &tree_entries[3].oid_hex).expect("Failed subtree parse");
        assert_eq!(sub_entries.len(), 1);
        assert_eq!(sub_entries[0].name, "main.c");
        assert_eq!(sub_entries[0].oid_hex, to_hex(&idx_entries[3].sha1));
        println!("Rust tree parser verified slash-sorted hierarchy: PASS");

        // 4. Packfile: every object, with SHA-1 and CRC32 checks inside read_pack_object
        let oids = list_idx_oids(idx_path).expect("Failed idx listing");
        let mut deltas = 0;
        for oid in &oids {
            let (p_type, _payload, is_delta) = read_pack_object(pack_path, idx_path, oid).expect("Failed pack read");
            assert_eq!(p_type, "blob");
            if is_delta {
                deltas += 1;
            }
        }
        println!(
            "Rust packfile engine verified {} objects ({} OFS_DELTA) with SHA-1 and CRC32 checks: PASS",
            oids.len(),
            deltas
        );

        // 5. Checkout into a fresh directory: contents and modes
        let restored = checkout_tree_recursive(repo, tree_oid, checkout_dir).expect("Failed checkout");
        assert_eq!(restored, 4);
        assert_eq!(fs::read(checkout_dir.join("hello.txt")).expect("Read hello.txt"), b"hello world\n");
        assert_eq!(fs::read(checkout_dir.join("build.sh")).expect("Read build.sh"), b"#!/bin/sh\necho building...\n");
        assert_eq!(fs::read(checkout_dir.join("src").join("main.c")).expect("Read main.c"), b"int main() { return 0; }\n");
        assert_eq!(fs::read(checkout_dir.join("src.txt")).expect("Read src.txt"), b"a plain file next to the src directory\n");
        let perm = |p: &str| fs::metadata(checkout_dir.join(p)).expect("stat").permissions().mode() & 0o777;
        assert_eq!(perm("build.sh"), 0o755);
        assert_eq!(perm("hello.txt"), 0o644);
        assert_eq!(perm("src/main.c"), 0o644);
        assert_eq!(perm("src.txt"), 0o644);
        println!("Rust checkout verified working tree restore and modes: PASS");
    }
}
