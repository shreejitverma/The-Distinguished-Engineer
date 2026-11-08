#!/usr/bin/env python3
import sys
import struct
import base64

def parse_cert(path):
    with open(path) as f:
        line = f.read().strip()
    parts = line.split()
    raw = base64.b64decode(parts[1])
    off = 0

    def get_str():
        nonlocal off
        l = struct.unpack(">I", raw[off:off+4])[0]
        v = raw[off+4:off+4+l]
        off += 4 + l
        return v

    def get_u32():
        nonlocal off
        v = struct.unpack(">I", raw[off:off+4])[0]
        off += 4
        return v

    def get_u64():
        nonlocal off
        v = struct.unpack(">Q", raw[off:off+8])[0]
        off += 8
        return v

    cert_type = get_str().decode("ascii", errors="replace")
    nonce = get_str()
    pk = get_str()
    if cert_type.startswith("sk-"):
        app = get_str().decode("ascii", errors="replace")
    serial = get_u64()
    kind = get_u32()
    key_id = get_str().decode("ascii", errors="replace")
    p_raw = get_str()
    principals = []
    poff = 0
    while poff < len(p_raw):
        pl = struct.unpack(">I", p_raw[poff:poff+4])[0]
        principals.append(p_raw[poff+4:poff+4+pl].decode("ascii", errors="replace"))
        poff += 4 + pl
    valid_after = get_u64()
    valid_before = get_u64()
    crit_raw = get_str()
    crit = []
    coff = 0
    while coff < len(crit_raw):
        cl = struct.unpack(">I", crit_raw[coff:coff+4])[0]
        cname = crit_raw[coff+4:coff+4+cl].decode("ascii", errors="replace")
        coff += 4 + cl
        cdlen = struct.unpack(">I", crit_raw[coff:coff+4])[0]
        cdata = crit_raw[coff+4:coff+4+cdlen]
        coff += 4 + cdlen
        if cdlen >= 4:
            vlen = struct.unpack(">I", cdata[:4])[0]
            val = cdata[4:4+vlen].decode("ascii", errors="replace")
            crit.append(f"{cname}={val}")
        elif cdata:
            crit.append(f"{cname}={cdata.decode('ascii', errors='replace')}")
        else:
            crit.append(cname)
    ext_raw = get_str()
    exts = []
    eoff = 0
    while eoff < len(ext_raw):
        el = struct.unpack(">I", ext_raw[eoff:eoff+4])[0]
        ename = ext_raw[eoff+4:eoff+4+el].decode("ascii", errors="replace")
        eoff += 4 + el
        edlen = struct.unpack(">I", ext_raw[eoff:eoff+4])[0]
        edata = ext_raw[eoff+4:eoff+4+edlen]
        eoff += 4 + edlen
        if edlen >= 4:
            vlen = struct.unpack(">I", edata[:4])[0]
            val = edata[4:4+vlen].decode("ascii", errors="replace")
            exts.append(f"{ename}={val}")
        elif edata:
            exts.append(f"{ename}={edata.decode('ascii', errors='replace')}")
        else:
            exts.append(ename)
    return {
        "cert_type": cert_type,
        "serial": serial,
        "kind": "user" if kind == 1 else "host",
        "key_id": key_id,
        "principals": principals,
        "valid_after": valid_after,
        "valid_before": valid_before,
        "critical_options": crit,
        "extensions": exts
    }

def parse_krl(path):
    data = open(path, "rb").read()
    if data[:8] != b"SSHKRL\n\0":
        raise ValueError("Invalid KRL magic header")
    off = 8
    fmt_ver = struct.unpack(">I", data[off:off+4])[0]
    off += 4
    krl_ver = struct.unpack(">Q", data[off:off+8])[0]
    off += 8
    gen_date = struct.unpack(">Q", data[off:off+8])[0]
    off += 8
    flags = struct.unpack(">Q", data[off:off+8])[0]
    off += 8
    # string reserved
    res_len = struct.unpack(">I", data[off:off+4])[0]
    off += 4 + res_len
    # string comment
    comm_len = struct.unpack(">I", data[off:off+4])[0]
    off += 4 + comm_len

    sections = []
    revoked_serials = []
    sec_names = []
    while off < len(data):
        sec_type = data[off]
        sec_len = struct.unpack(">I", data[off+1:off+5])[0]
        payload = data[off+5:off+5+sec_len]
        off += 5 + sec_len

        if sec_type == 1:
            sec_names.append("KRL_SECTION_CERTIFICATES")
            ca_len = struct.unpack(">I", payload[:4])[0]
            p_off = 4 + ca_len
            res2_len = struct.unpack(">I", payload[p_off:p_off+4])[0]
            p_off += 4 + res2_len
            while p_off < len(payload):
                sub_type = payload[p_off]
                sub_len = struct.unpack(">I", payload[p_off+1:p_off+5])[0]
                sub_data = payload[p_off+5:p_off+5+sub_len]
                p_off += 5 + sub_len
                if sub_type == 0x20:
                    for i in range(0, len(sub_data), 8):
                        revoked_serials.append(struct.unpack(">Q", sub_data[i:i+8])[0])
        elif sec_type == 2:
            sec_names.append("KRL_SECTION_EXPLICIT_KEY")
        elif sec_type == 3:
            sec_names.append("KRL_SECTION_FINGERPRINT_SHA1")
        elif sec_type == 5:
            sec_names.append("KRL_SECTION_FINGERPRINT_SHA256")
        else:
            sec_names.append(f"KRL_SECTION_{sec_type}")

    return {
        "magic": "SSHKRL",
        "format_version": fmt_ver,
        "krl_version": krl_ver,
        "section_types": sec_names,
        "revoked_serials": revoked_serials
    }

def main():
    if len(sys.argv) < 3:
        print("Usage: lab_inspect.py <cert|krl> <file>")
        sys.exit(1)
    mode = sys.argv[1]
    target = sys.argv[2]
    if mode == "cert":
        c = parse_cert(target)
        print(f"  Type: {c['kind']} certificate ({c['cert_type']})")
        print(f"  Key ID: \"{c['key_id']}\"")
        print(f"  Serial: {c['serial']}")
        print(f"  Principals: {', '.join(c['principals']) if c['principals'] else '(none)'}")
        print(f"  Critical Options: {', '.join(c['critical_options']) if c['critical_options'] else 'none'}")
        print(f"  Extensions: {', '.join(c['extensions']) if c['extensions'] else 'none'}")
    elif mode == "krl":
        k = parse_krl(target)
        print(f"KRL Magic Header: {k['magic']}")
        print(f"KRL Format Version: {k['format_version']}")
        print(f"KRL Section Types: {', '.join(k['section_types'])}")
        serials_str = ", ".join(str(s) for s in k["revoked_serials"]) if k["revoked_serials"] else "none"
        print(f"Revoked Serials: {serials_str}")

if __name__ == "__main__":
    main()
