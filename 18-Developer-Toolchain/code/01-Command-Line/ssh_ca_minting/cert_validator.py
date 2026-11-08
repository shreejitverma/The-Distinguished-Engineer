#!/usr/bin/env python3
import base64
import hashlib
import struct
import sys

# RFC 8032 Ed25519 pure Python verification
q = 2**255 - 19
l = 2**252 + 27742317777372353535851937790883648493

def inv(z):
    return pow(z, q - 2, q)

d = -121665 * inv(121666) % q
I = pow(2, (q - 1) // 4, q)

def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * I) % q
    if x % 2 != 0:
        x = q - x
    return x

By = 4 * inv(5) % q
Bx = xrecover(By)
B = [Bx, By]

def edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * inv(1 + d * x1 * x2 * y1 * y2) % q
    y3 = (y1 * y2 + x1 * x2) * inv(1 - d * x1 * x2 * y1 * y2) % q
    return [x3, y3]

def scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = scalarmult(P, e // 2)
    Q = edwards(Q, Q)
    if e & 1:
        Q = edwards(Q, P)
    return Q

def decodepoint(s):
    y = int.from_bytes(s, 'little')
    clamp_y = y & ((1 << 255) - 1)
    x = xrecover(clamp_y)
    if (x & 1) != (y >> 255):
        x = q - x
    return [x, clamp_y]

def ed25519_verify(sig, msg, pubkey):
    if len(sig) != 64 or len(pubkey) != 32:
        return False
    try:
        R = decodepoint(sig[:32])
        A = decodepoint(pubkey)
        S = int.from_bytes(sig[32:], 'little')
        if S >= l:
            return False
        h = int.from_bytes(hashlib.sha512(sig[:32] + pubkey + msg).digest(), 'little')
        return scalarmult(B, S) == edwards(R, scalarmult(A, h))
    except Exception:
        return False

def parse_cert(cert_path):
    with open(cert_path, 'r', encoding='ascii') as f:
        content = f.read().strip()
    parts = content.split()
    raw = base64.b64decode(parts[1])

    pos = 0
    def get_string():
        nonlocal pos
        (slen,) = struct.unpack_from('>I', raw, pos)
        pos += 4
        val = raw[pos:pos+slen]
        pos += slen
        return val

    cert_type = get_string().decode('ascii')
    nonce = get_string()
    pubkey = get_string()
    (serial, cert_role) = struct.unpack_from('>QI', raw, pos)
    pos += 12
    key_id_offset = pos + 4
    key_id = get_string().decode('ascii')
    principals_raw = get_string()
    (valid_after, valid_before) = struct.unpack_from('>QQ', raw, pos)
    pos += 16
    critical_options_raw = get_string()
    extensions_raw = get_string()
    reserved = get_string()

    ca_key_blob = get_string()
    signed_len = pos
    sig_blob = get_string()

    # Parse principals
    p_pos = 0
    principals = []
    while p_pos < len(principals_raw):
        (plen,) = struct.unpack_from('>I', principals_raw, p_pos)
        p_pos += 4
        principals.append(principals_raw[p_pos:p_pos+plen].decode('ascii'))
        p_pos += plen

    # Parse critical options
    co_pos = 0
    critical_options = []
    while co_pos < len(critical_options_raw):
        (oname_len,) = struct.unpack_from('>I', critical_options_raw, co_pos)
        co_pos += 4
        oname = critical_options_raw[co_pos:co_pos+oname_len].decode('ascii')
        co_pos += oname_len
        (oval_len,) = struct.unpack_from('>I', critical_options_raw, co_pos)
        co_pos += 4
        oval = critical_options_raw[co_pos:co_pos+oval_len]
        co_pos += oval_len
        critical_options.append((oname, oval))

    # Parse extensions
    ext_pos = 0
    extensions = []
    while ext_pos < len(extensions_raw):
        (ename_len,) = struct.unpack_from('>I', extensions_raw, ext_pos)
        ext_pos += 4
        ename = extensions_raw[ext_pos:ext_pos+ename_len].decode('ascii')
        ext_pos += ename_len
        (eval_len,) = struct.unpack_from('>I', extensions_raw, ext_pos)
        ext_pos += 4
        eval_data = extensions_raw[ext_pos:ext_pos+eval_len]
        ext_pos += eval_len
        extensions.append((ename, eval_data))

    # Parse CA key
    (ca_type_len,) = struct.unpack_from('>I', ca_key_blob, 0)
    ca_type = ca_key_blob[4:4+ca_type_len].decode('ascii')
    ca_pubkey = ca_key_blob[4+ca_type_len+4:]

    # Parse signature
    (sig_type_len,) = struct.unpack_from('>I', sig_blob, 0)
    sig_type = sig_blob[4:4+sig_type_len].decode('ascii')
    raw_sig = sig_blob[4+sig_type_len+4:]

    signed_bytes = raw[:signed_len]
    is_valid = ed25519_verify(raw_sig, signed_bytes, ca_pubkey)

    return {
        'cert_type': cert_type,
        'nonce_len': len(nonce),
        'pubkey_len': len(pubkey),
        'serial': serial,
        'cert_role': cert_role,
        'key_id': key_id,
        'key_id_offset': key_id_offset,
        'principals': principals,
        'valid_after': valid_after,
        'valid_before': valid_before,
        'critical_options': critical_options,
        'extensions': extensions,
        'ca_type': ca_type,
        'ca_pubkey_len': len(ca_pubkey),
        'sig_type': sig_type,
        'raw_sig_len': len(raw_sig),
        'sig_valid': is_valid,
        'raw_bytes': raw,
        'signed_len': signed_len,
        'raw_sig': raw_sig,
        'ca_pubkey': ca_pubkey,
    }

def cmd_wire(cert_path):
    u = parse_cert(cert_path)
    print("=== 3. Binary Wire Format Verification ===")
    print(f"Wire protocol certificate type: {u['cert_type']}")
    print(f"Nonce length: {u['nonce_len']} bytes (present)")
    print(f"Public key bytes length: {u['pubkey_len']} bytes")
    print(f"Serial: {u['serial']}")
    role_str = "SSH2_CERT_TYPE_USER" if u['cert_role'] == 1 else "SSH2_CERT_TYPE_HOST"
    print(f"Role type: {u['cert_role']} ({role_str})")
    print(f"Key ID: {u['key_id']}")
    print(f"Principals list: {u['principals']}")
    duration = u['valid_before'] - u['valid_after']
    print(f"Validity window: {u['valid_after']} to {u['valid_before']} (duration: {duration}s)")
    print(f"Critical options count: {len(u['critical_options'])}")
    print(f"Extensions count: {len(u['extensions'])}")
    print(f"CA public key bytes length: {u['ca_pubkey_len']} bytes")
    print(f"Signature algorithm: {u['sig_type']}")
    print(f"Signature length: {u['raw_sig_len']} bytes")
    print(f"Ed25519 cryptographic CA signature: {'VALID' if u['sig_valid'] else 'INVALID'}")


def cmd_tamper(cert_path, out_path):
    """Flip the low bit of the first Key ID byte and re-verify the CA signature."""
    u = parse_cert(cert_path)
    raw = bytearray(u['raw_bytes'])
    off = u['key_id_offset']
    assert raw[off:off + len(u['key_id'])] == u['key_id'].encode('ascii')
    raw[off] ^= 0x01
    valid = ed25519_verify(u['raw_sig'], bytes(raw[:u['signed_len']]), u['ca_pubkey'])
    print(f"Flipped bit 0 of byte {off} (first byte of Key ID '{u['key_id']}')")
    print(f"Pure-Python Ed25519 check of the tampered bytes: {'SIGNATURE_VALID' if valid else 'SIGNATURE_INVALID'}")
    with open(cert_path, 'r', encoding='ascii') as f:
        key_type = f.read().split()[0]
    with open(out_path, 'w', encoding='ascii') as f:
        f.write(key_type + ' ' + base64.b64encode(bytes(raw)).decode('ascii') + '\n')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'wire':
        cmd_wire(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == 'tamper':
        cmd_tamper(sys.argv[2], sys.argv[3])
    else:
        print("Usage: cert_validator.py wire <cert> | tamper <cert> <out-cert>")
        sys.exit(1)


if __name__ == '__main__':
    main()
