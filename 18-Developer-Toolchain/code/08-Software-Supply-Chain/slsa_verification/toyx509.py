"""Minimal DER encoder/decoder for the X.509 subset Fulcio uses, plus openssl ECDSA wrappers.

Hand-building certificates keeps the lab deterministic (fixed validity windows) and
portable (no `openssl x509 -not_before`, which older OpenSSL releases lack).
Only the openssl CLI is used for key generation, signing, and verification.
"""

import base64
import datetime
import hashlib
import os
import subprocess

OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
OID_CN = "2.5.4.3"
OID_KEY_USAGE = "2.5.29.15"
OID_SAN = "2.5.29.17"
OID_BASIC_CONSTRAINTS = "2.5.29.19"
OID_EKU = "2.5.29.37"
OID_EKU_CODE_SIGNING = "1.3.6.1.5.5.7.3.3"
FULCIO = "1.3.6.1.4.1.57264.1."
OID_ISSUER_V2 = FULCIO + "8"
OID_BUILD_SIGNER_URI = FULCIO + "9"
OID_RUNNER_ENV = FULCIO + "11"
OID_SOURCE_REPO_URI = FULCIO + "12"
OID_SOURCE_REPO_DIGEST = FULCIO + "13"
OID_SOURCE_REPO_REF = FULCIO + "14"
OID_BUILD_TRIGGER = FULCIO + "20"


def der_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, content):
    return bytes([tag]) + der_len(len(content)) + content


def seq(*parts):
    return tlv(0x30, b"".join(parts))


def oid(dotted):
    p = [int(x) for x in dotted.split(".")]
    out = bytearray([40 * p[0] + p[1]])
    for n in p[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        out.extend(reversed(chunk))
    return tlv(0x06, bytes(out))


def integer(n):
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big")
    return tlv(0x02, b)


def utf8(s):
    return tlv(0x0C, s.encode("utf-8"))


def utctime(dt):
    return tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def octets(b):
    return tlv(0x04, b)


def boolean(v):
    return tlv(0x01, b"\xff" if v else b"\x00")


def name(cn):
    return seq(tlv(0x31, seq(oid(OID_CN), utf8(cn))))


def extension(dotted, value, critical=False):
    return seq(oid(dotted), boolean(True) if critical else b"", octets(value))


def read_tlv(buf, off):
    tag = buf[off]
    first = buf[off + 1]
    if first & 0x80:
        n = first & 0x7F
        length = int.from_bytes(buf[off + 2 : off + 2 + n], "big")
        start = off + 2 + n
    else:
        length = first
        start = off + 2
    return tag, start, start + length


def children(buf, start, end):
    out = []
    off = start
    while off < end:
        tag, cs, ce = read_tlv(buf, off)
        out.append((tag, cs, ce, off))
        off = ce
    return out


def decode_oid(content):
    first = content[0]
    parts = [first // 40, first % 40]
    n = 0
    for byte in content[1:]:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(n)
            n = 0
    return ".".join(str(p) for p in parts)


def parse_time(buf, tag, cs, ce):
    text = buf[cs:ce].decode("ascii")
    fmt = "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ"
    return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)


def parse_cert(der):
    """Return the fields the verifier needs from a certificate DER."""
    _, cs, ce = read_tlv(der, 0)
    tbs_tag, tbs_cs, tbs_ce = read_tlv(der, cs)
    kids = children(der, cs, ce)
    tbs_full = der[kids[0][3] : kids[0][2]]
    sig_bits = der[kids[2][1] : kids[2][2]][1:]
    fields = children(der, tbs_cs, tbs_ce)
    i = 0
    if fields[0][0] == 0xA0:
        i = 1
    issuer = der[fields[i + 2][3] : fields[i + 2][2]]
    validity = children(der, fields[i + 3][1], fields[i + 3][2])
    not_before = parse_time(der, *validity[0][:3])
    not_after = parse_time(der, *validity[1][:3])
    subject = der[fields[i + 4][3] : fields[i + 4][2]]
    spki = der[fields[i + 5][3] : fields[i + 5][2]]
    exts = {}
    ext_seq = children(der, fields[-1][1], fields[-1][2])[0]
    for tag, xs, xe, _ in children(der, ext_seq[1], ext_seq[2]):
        parts = children(der, xs, xe)
        ext_oid = decode_oid(der[parts[0][1] : parts[0][2]])
        critical = len(parts) == 3 and der[parts[1][1] : parts[1][2]] != b"\x00"
        exts[ext_oid] = (critical, der[parts[-1][1] : parts[-1][2]])
    return {
        "tbs": tbs_full,
        "sig": sig_bits,
        "issuer": issuer,
        "subject": subject,
        "spki": spki,
        "not_before": not_before,
        "not_after": not_after,
        "exts": exts,
    }


def ext_string(cert, dotted):
    """Value of a Fulcio v2 extension (DER UTF8String inside the OCTET STRING), or None."""
    if dotted not in cert["exts"]:
        return None
    v = cert["exts"][dotted][1]
    tag, cs, ce = read_tlv(v, 0)
    assert tag == 0x0C, "Fulcio extensions 8 and above carry a UTF8String"
    return v[cs:ce].decode("utf-8")


def san_uris(cert):
    if OID_SAN not in cert["exts"]:
        return []
    v = cert["exts"][OID_SAN][1]
    _, cs, ce = read_tlv(v, 0)
    return [v[a:b].decode("ascii") for tag, a, b, _ in children(v, cs, ce) if tag == 0x86]


def has_code_signing_eku(cert):
    if OID_EKU not in cert["exts"]:
        return False
    v = cert["exts"][OID_EKU][1]
    _, cs, ce = read_tlv(v, 0)
    return any(decode_oid(v[a:b]) == OID_EKU_CODE_SIGNING for tag, a, b, _ in children(v, cs, ce) if tag == 0x06)


def is_ca(cert):
    if OID_BASIC_CONSTRAINTS not in cert["exts"]:
        return False
    v = cert["exts"][OID_BASIC_CONSTRAINTS][1]
    _, cs, ce = read_tlv(v, 0)
    kids = children(v, cs, ce)
    return bool(kids) and kids[0][0] == 0x01 and v[kids[0][1]] != 0


def build_cert(issuer_cn, subject_cn, spki, not_before, not_after, serial, signer, extensions):
    """Build and sign a v3 certificate. `signer` signs the TBS bytes and returns an ECDSA DER signature."""
    alg = seq(oid(OID_ECDSA_SHA256))
    tbs = seq(
        tlv(0xA0, integer(2)),
        integer(serial),
        alg,
        name(issuer_cn),
        seq(utctime(not_before), utctime(not_after)),
        name(subject_cn) if subject_cn else seq(),
        spki,
        tlv(0xA3, seq(*extensions)),
    )
    return seq(tbs, alg, tlv(0x03, b"\x00" + signer(tbs)))


# ---- openssl wrappers -------------------------------------------------------


def _run(args, **kw):
    return subprocess.run(["openssl", *args], capture_output=True, check=False, **kw)


def gen_key(path):
    r = _run(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", path])
    if r.returncode != 0:
        raise RuntimeError("openssl ecparam failed: " + r.stderr.decode())
    return path


def public_der(key_path):
    r = _run(["pkey", "-in", key_path, "-pubout", "-outform", "DER"])
    if r.returncode != 0:
        raise RuntimeError("openssl pkey failed: " + r.stderr.decode())
    return r.stdout


def pem_public(spki_der):
    b64 = base64.encodebytes(spki_der).decode("ascii").replace("\n", "")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def sign(key_path, data, workdir):
    dpath = os.path.join(workdir, "sign.in")
    spath = os.path.join(workdir, "sign.out")
    with open(dpath, "wb") as f:
        f.write(data)
    r = _run(["dgst", "-sha256", "-sign", key_path, "-out", spath, dpath])
    if r.returncode != 0:
        raise RuntimeError("openssl dgst -sign failed: " + r.stderr.decode())
    with open(spath, "rb") as f:
        return f.read()


def verify(spki_der, data, sig, workdir):
    ppath = os.path.join(workdir, "verify.pub")
    dpath = os.path.join(workdir, "verify.in")
    spath = os.path.join(workdir, "verify.sig")
    with open(ppath, "w") as f:
        f.write(pem_public(spki_der))
    with open(dpath, "wb") as f:
        f.write(data)
    with open(spath, "wb") as f:
        f.write(sig)
    r = _run(["dgst", "-sha256", "-verify", ppath, "-signature", spath, dpath])
    return r.returncode == 0 and b"Verified OK" in r.stdout


def openssl_text(cert_der, workdir):
    path = os.path.join(workdir, "cert.der")
    with open(path, "wb") as f:
        f.write(cert_der)
    r = _run(["x509", "-inform", "DER", "-in", path, "-noout", "-text"])
    if r.returncode != 0:
        raise RuntimeError("openssl x509 rejected the hand-built certificate: " + r.stderr.decode())
    return r.stdout.decode("utf-8", "replace")


def sha256_hex(b):
    return hashlib.sha256(b).hexdigest()
