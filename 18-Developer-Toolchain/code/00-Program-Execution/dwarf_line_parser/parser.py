"""Decode DWARF .debug_line from ELF relocatable objects and cross-check it against GNU readelf.

Supports DWARF 2 to 5 line headers in little-endian ELF64 relocatable objects with 32-bit DWARF (4-byte offsets).
Limits, all rejected with ValueError rather than guessed: big-endian or 32-bit ELF, 64-bit DWARF,
VLIW (max_ops > 1), unknown DW_FORM codes in DWARF 5 entry formats.
"""

import os
import re
import struct
import subprocess
import sys

# DWARF 5 section 6.2.4.1 content type codes and section 7.5.6 form codes used by line headers.
DW_LNCT_PATH = 1
DW_LNCT_DIRECTORY_INDEX = 2
DW_FORM_BLOCK = 0x09
DW_FORM_DATA1 = 0x0B
DW_FORM_DATA2 = 0x05
DW_FORM_DATA4 = 0x06
DW_FORM_DATA8 = 0x07
DW_FORM_STRING = 0x08
DW_FORM_STRP = 0x0E
DW_FORM_UDATA = 0x0F
DW_FORM_DATA16 = 0x1E
DW_FORM_LINE_STRP = 0x1F


def read_uleb128(data, offset):
    res = 0
    shift = 0
    while True:
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, offset


def read_sleb128(data, offset):
    res = 0
    shift = 0
    while True:
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            if (shift < 64) and (b & 0x40):
                res |= -(1 << shift)
            break
    return res, offset


def read_cstr(data, offset):
    end = data.find(b"\x00", offset)
    if end < 0:
        raise ValueError(f"Unterminated string at byte {offset}")
    return data[offset:end].decode("ascii", errors="replace"), end + 1


def parse_elf(data):
    """Return (sections, relocs) for a little-endian ELF64 file given as bytes.

    relocs maps a byte offset inside .debug_line to the RELA addend that targets it.
    In a relocatable object the linker has not run, so section content holds zero where a RELA
    relocation will later store a value, and the addend carries the real offset.
    """
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError("Not an ELF object file")
    if data[4] != 2 or data[5] != 1:
        raise ValueError("Unsupported ELF: only little-endian ELF64 is handled")

    e_shoff = struct.unpack("<Q", data[40:48])[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack("<HHH", data[58:64])

    def header(i):
        base = e_shoff + i * e_shentsize
        sh = data[base : base + e_shentsize]
        if len(sh) < 40 or e_shentsize < 40:
            raise ValueError("Truncated ELF section header table")
        name_idx = struct.unpack("<I", sh[0:4])[0]
        offset, size = struct.unpack("<QQ", sh[24:40])
        return name_idx, offset, size

    _, str_off, str_size = header(e_shstrndx)
    shstrtab = data[str_off : str_off + str_size]

    sections = {}
    for i in range(e_shnum):
        name_idx, offset, size = header(i)
        name = shstrtab[name_idx:].split(b"\x00")[0].decode("ascii", errors="replace")
        sections[name] = data[offset : offset + size]

    relocs = {}
    rela = sections.get(".rela.debug_line", b"")
    for pos in range(0, len(rela) - 23, 24):
        r_offset, _r_info, r_addend = struct.unpack("<QQq", rela[pos : pos + 24])
        relocs[r_offset] = r_addend
    return sections, relocs


def read_form(data, offset, form, sections, relocs):
    """Read one DWARF 5 line-header attribute value; 32-bit DWARF offsets only."""
    if form == DW_FORM_STRING:
        return read_cstr(data, offset)
    if form in (DW_FORM_LINE_STRP, DW_FORM_STRP):
        raw = struct.unpack("<I", data[offset : offset + 4])[0]
        str_off = raw + relocs.get(offset, 0)
        section = sections.get(".debug_line_str" if form == DW_FORM_LINE_STRP else ".debug_str", b"")
        name, _ = read_cstr(section, str_off)
        return name, offset + 4
    if form == DW_FORM_UDATA:
        return read_uleb128(data, offset)
    fixed = {DW_FORM_DATA1: 1, DW_FORM_DATA2: 2, DW_FORM_DATA4: 4, DW_FORM_DATA8: 8, DW_FORM_DATA16: 16}
    if form in fixed:
        n = fixed[form]
        return int.from_bytes(data[offset : offset + n], "little"), offset + n
    if form == DW_FORM_BLOCK:
        n, offset = read_uleb128(data, offset)
        return data[offset : offset + n], offset + n
    raise ValueError(f"Unsupported DW_FORM 0x{form:x} in line header entry format")


def read_v5_entries(data, offset, sections, relocs):
    """Read one DWARF 5 entry-format description plus its entries; return ([{type: value}], offset)."""
    fmt_count = data[offset]
    offset += 1
    formats = []
    for _ in range(fmt_count):
        content_type, offset = read_uleb128(data, offset)
        form, offset = read_uleb128(data, offset)
        formats.append((content_type, form))
    count, offset = read_uleb128(data, offset)
    entries = []
    for _ in range(count):
        entry = {}
        for content_type, form in formats:
            entry[content_type], offset = read_form(data, offset, form, sections, relocs)
        entries.append(entry)
    return entries, offset


def header_field_offset(version):
    """Offset of header_length in a 32-bit DWARF line header; the fields after it follow in order."""
    return 4 + 2 + (2 if version == 5 else 0)


def parse_debug_line(data, sections=None, relocs=None):
    sections = sections or {}
    relocs = relocs or {}
    if len(data) < 16:
        raise ValueError("Section too short to contain a valid DWARF line header")
    offset = 0
    unit_length = struct.unpack("<I", data[offset : offset + 4])[0]
    offset += 4
    if unit_length == 0xFFFFFFFF:
        raise ValueError("64-bit DWARF is not supported")
    total_length = offset + unit_length
    if total_length > len(data):
        raise ValueError(f"Declared unit length ({unit_length}) exceeds section size ({len(data)})")

    version = struct.unpack("<H", data[offset : offset + 2])[0]
    offset += 2
    if version not in (2, 3, 4, 5):
        raise ValueError(f"Unsupported DWARF version: {version}")

    if version == 5:
        address_size = data[offset]
        offset += 2  # address_size, segment_selector_size
        if address_size not in (4, 8):
            raise ValueError(f"Unsupported address size: {address_size}")
    else:
        address_size = 8  # ELF64 object

    header_length = struct.unpack("<I", data[offset : offset + 4])[0]
    offset += 4
    header_end = offset + header_length
    if header_end > total_length:
        raise ValueError("header_length runs past the end of the unit")

    min_inst_len = data[offset]
    max_ops = data[offset + 1]
    default_is_stmt = data[offset + 2]
    line_base = struct.unpack("<b", bytes([data[offset + 3]]))[0]
    line_range = data[offset + 4]
    opcode_base = data[offset + 5]
    offset += 6

    if max_ops != 1:
        raise ValueError(f"Unsupported maximum_operations_per_instruction: {max_ops} (VLIW)")
    if line_range == 0:
        raise ValueError("Invalid line_range: 0 (division by zero in special opcode decoding)")

    std_opcode_lens = list(data[offset : offset + opcode_base - 1])
    offset += opcode_base - 1

    directories = []
    files = []

    if version <= 4:
        while data[offset] != 0:
            dir_name, offset = read_cstr(data, offset)
            directories.append(dir_name)
        offset += 1

        while data[offset] != 0:
            file_name, offset = read_cstr(data, offset)
            dir_idx, offset = read_uleb128(data, offset)
            _mtime, offset = read_uleb128(data, offset)
            _flen, offset = read_uleb128(data, offset)
            files.append((file_name, dir_idx))
        offset += 1
        file_base = 1
    else:
        dir_entries, offset = read_v5_entries(data, offset, sections, relocs)
        directories = [d.get(DW_LNCT_PATH, "") for d in dir_entries]
        file_entries, offset = read_v5_entries(data, offset, sections, relocs)
        files = [(f.get(DW_LNCT_PATH, ""), f.get(DW_LNCT_DIRECTORY_INDEX, 0)) for f in file_entries]
        file_base = 0

    if offset != header_end:
        raise ValueError(f"Header tables end at byte {offset} but header_length ends the header at byte {header_end}")

    # Execute Line Number State Machine
    offset = header_end
    address = 0
    file_idx = 1
    line = 1
    column = 0
    is_stmt = bool(default_is_stmt)
    basic_block = False
    discriminator = 0

    matrix = []
    instructions_decoded = 0

    def emit(end_sequence):
        matrix.append({
            "address": address, "file": file_idx, "line": line,
            "column": column, "is_stmt": is_stmt, "basic_block": basic_block,
            "end_sequence": end_sequence, "discriminator": discriminator
        })

    try:
        while offset < total_length:
            op = data[offset]
            offset += 1
            instructions_decoded += 1

            if op == 0:
                # Extended opcode
                ext_len, offset = read_uleb128(data, offset)
                ext_op = data[offset]
                offset += 1
                if ext_op == 1:  # DW_LNE_end_sequence
                    emit(True)
                    address = 0
                    file_idx = 1
                    line = 1
                    column = 0
                    is_stmt = bool(default_is_stmt)
                    basic_block = False
                    discriminator = 0
                elif ext_op == 2:  # DW_LNE_set_address
                    address = int.from_bytes(data[offset : offset + address_size], "little")
                    offset += address_size
                elif ext_op == 4:  # DW_LNE_set_discriminator
                    discriminator, offset = read_uleb128(data, offset)
                else:
                    offset += ext_len - 1
            elif op < opcode_base:
                # Standard opcode
                if op == 1:  # DW_LNS_copy
                    emit(False)
                    basic_block = False
                    discriminator = 0
                elif op == 2:  # DW_LNS_advance_pc
                    adv, offset = read_uleb128(data, offset)
                    address += adv * min_inst_len
                elif op == 3:  # DW_LNS_advance_line
                    delta, offset = read_sleb128(data, offset)
                    line += delta
                elif op == 4:  # DW_LNS_set_file
                    file_idx, offset = read_uleb128(data, offset)
                elif op == 5:  # DW_LNS_set_column
                    column, offset = read_uleb128(data, offset)
                elif op == 6:  # DW_LNS_negate_stmt
                    is_stmt = not is_stmt
                elif op == 7:  # DW_LNS_set_basic_block
                    basic_block = True
                elif op == 8:  # DW_LNS_const_add_pc
                    address += ((255 - opcode_base) // line_range) * min_inst_len
                elif op == 9:  # DW_LNS_fixed_advance_pc
                    address += struct.unpack("<H", data[offset : offset + 2])[0]
                    offset += 2
                elif op in (10, 11):  # DW_LNS_set_prologue_end, DW_LNS_set_epilogue_begin
                    pass
                elif op == 12:  # DW_LNS_set_isa
                    _isa, offset = read_uleb128(data, offset)
                else:
                    for _ in range(std_opcode_lens[op - 1]):
                        _, offset = read_uleb128(data, offset)
            else:
                # Special opcode
                adj = op - opcode_base
                line += line_base + (adj % line_range)
                address += (adj // line_range) * min_inst_len
                emit(False)
                basic_block = False
                discriminator = 0
    except (IndexError, ValueError, struct.error) as exc:
        raise ValueError(f"Line number program is truncated or malformed: {exc}") from exc

    if offset != total_length:
        raise ValueError(f"Line number program overran its unit: ended at byte {offset}, unit ends at {total_length}")

    return {
        "version": version,
        "unit_length": unit_length,
        "prologue_length": header_length,
        "min_inst_len": min_inst_len,
        "max_ops": max_ops,
        "default_is_stmt": default_is_stmt,
        "line_base": line_base,
        "line_range": line_range,
        "opcode_base": opcode_base,
        "std_opcode_lens": std_opcode_lens,
        "directories": directories,
        "files": files,
        "file_base": file_base,
        "instructions_decoded": instructions_decoded,
        "matrix": matrix
    }


def decoded_rows(res):
    """Matrix rows as (file basename, line or '-', address, is_stmt) tuples, the way readelf prints them."""
    rows = []
    for r in res["matrix"]:
        name = res["files"][r["file"] - res["file_base"]][0]
        end = r["end_sequence"]
        rows.append((os.path.basename(name), "-" if end else str(r["line"]), r["address"], False if end else r["is_stmt"]))
    return rows


ROW_RE = re.compile(r"^(?P<file>\S.*?)\s+(?P<line>\d+|-)\s+(?P<addr>0x[0-9a-fA-F]+|0)(?:\s+(?P<rest>.*))?$")


def parse_readelf_decoded_rows(binary_path):
    """Rows of `readelf --debug-dump=decodedline` as (file, line or '-', address, is_stmt) tuples."""
    proc = subprocess.run(["readelf", "--debug-dump=decodedline", binary_path],
                          capture_output=True, text=True, check=True)
    rows = []
    for text in proc.stdout.splitlines():
        m = ROW_RE.match(text.strip())
        if m:
            stmt = "x" in (m.group("rest") or "").split()
            rows.append((os.path.basename(m.group("file")), m.group("line"), int(m.group("addr"), 16), stmt))
    return rows


def run_negative_tests(raw_line_data, raw_elf_data, version, sections, relocs):
    """Each case must raise ValueError whose message contains the expected text; anything else is a FAIL."""
    hdr = header_field_offset(version)

    def with_byte(pos, value):
        b = bytearray(raw_line_data)
        b[pos] = value
        return bytes(b)

    def with_u32(pos, value):
        b = bytearray(raw_line_data)
        b[pos : pos + 4] = struct.pack("<I", value)
        return bytes(b)

    line_cases = [
        ("truncated header", raw_line_data[:8], "too short"),
        ("zero line_range", with_byte(hdr + 8, 0), "line_range"),
        ("unit_length past section", with_u32(0, len(raw_line_data)), "exceeds section size"),
        ("unknown version", with_byte(4, 9), "Unsupported DWARF version"),
        ("VLIW max_ops", with_byte(hdr + 5, 2), "maximum_operations_per_instruction"),
        ("header_length off by one", with_u32(hdr, struct.unpack("<I", raw_line_data[hdr : hdr + 4])[0] + 1), "header_length"),
    ]
    results = []
    for name, blob, needle in line_cases:
        results.append((name, expect_value_error(lambda blob=blob: parse_debug_line(blob, sections, relocs), needle)))
    bad_magic = b"\x7fELG" + raw_elf_data[4:]
    results.append(("bad ELF magic", expect_value_error(lambda: parse_elf(bad_magic), "Not an ELF")))
    elf32 = raw_elf_data[:4] + b"\x01" + raw_elf_data[5:]
    results.append(("32-bit ELF", expect_value_error(lambda: parse_elf(elf32), "only little-endian ELF64")))
    big_endian = raw_elf_data[:5] + b"\x02" + raw_elf_data[6:]
    results.append(("big-endian ELF", expect_value_error(lambda: parse_elf(big_endian), "only little-endian ELF64")))
    return results


def expect_value_error(fn, needle):
    try:
        fn()
    except ValueError as exc:
        return needle in str(exc)
    except Exception:  # wrong exception type (ZeroDivisionError, IndexError, ...) is a failed check
        return False
    return False


def report(obj_file):
    """Print the report for one object file and return True when every check passed."""
    with open(obj_file, "rb") as f:
        elf_bytes = f.read()
    sections, relocs = parse_elf(elf_bytes)
    if ".debug_line" not in sections:
        raise ValueError(f".debug_line not found in {obj_file}")

    raw_line = sections[".debug_line"]
    res = parse_debug_line(raw_line, sections, relocs)

    print(f"--- Object file: {os.path.basename(obj_file)} ---")
    print(f"ELF section: .debug_line (size: {len(raw_line)} bytes)")
    print("Header:")
    print(f"  DWARF Version: {res['version']}")
    print(f"  Prologue length: {res['prologue_length']}")
    print(f"  Min instruction length: {res['min_inst_len']}")
    print(f"  Max ops per instruction: {res['max_ops']}")
    print(f"  Default is_stmt: {res['default_is_stmt']}")
    print(f"  Line base: {res['line_base']}")
    print(f"  Line range: {res['line_range']}")
    print(f"  Opcode base: {res['opcode_base']}")
    print(f"  Standard opcode lengths: {res['std_opcode_lens']}")
    print(f"  Directories: {len(res['directories'])} entries")
    print(f"  Files: {len(res['files'])} entries")
    for idx, (fname, didx) in enumerate(res["files"], res["file_base"]):
        print(f"    [{idx}] {fname} (dir={didx})")

    matrix = res["matrix"]
    print("State Machine Execution:")
    print(f"  Decoded instructions: {res['instructions_decoded']}")
    print(f"  Emitted matrix rows: {len(matrix)}")
    checks = []
    if matrix:
        min_addr = matrix[0]["address"]
        max_addr = matrix[-1]["address"]
        print(f"  Address range: 0x{min_addr:x} - 0x{max_addr:x} ({max_addr - min_addr} bytes)")
        min_line = min(r["line"] for r in matrix)
        max_line = max(r["line"] for r in matrix)
        print(f"  Line span: {min_line} - {max_line}")
    checks.append(("last row ends the sequence", bool(matrix) and matrix[-1]["end_sequence"]))

    print("Matrix Row Spot Checks:")
    for idx in [0, 1, 3, 6, len(matrix) - 1]:
        if idx < len(matrix):
            r = matrix[idx]
            end_flag = " end_seq=1" if r["end_sequence"] else ""
            print(f"  Row {idx:2d}: addr=0x{r['address']:02x} line={r['line']:2d} col={r['column']:2d} is_stmt={int(r['is_stmt'])}{end_flag}")

    ours = decoded_rows(res)
    theirs = parse_readelf_decoded_rows(obj_file)
    matched = sum(1 for a, b in zip(ours, theirs) if a == b)
    print("Cross-Tool Verification:")
    print(f"  GNU readelf decoded rows: {len(theirs)}")
    print(f"  Rows equal on (file, line, address, is_stmt): {matched} of {len(ours)}")
    checks.append(("readelf rows equal", len(ours) == len(theirs) and matched == len(ours) and bool(ours)))

    print("Integrity and Error Checks:")
    for name, ok in run_negative_tests(raw_line, elf_bytes, res["version"], sections, relocs):
        print(f"  Rejects {name}: {'PASS' if ok else 'FAIL'}")
        checks.append((f"rejects {name}", ok))
    for name, ok in checks:
        if not ok:
            print(f"  Check failed: {name}")
    return all(ok for _, ok in checks)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 parser.py <elf-object-file>...")
        sys.exit(2)

    print("=== DWARF Line Table Parser Verification ===")
    all_ok = True
    for obj_file in sys.argv[1:]:
        try:
            all_ok = report(obj_file) and all_ok
        except ValueError as exc:
            print(f"Error: {exc}")
            all_ok = False
    print(f"Verification Result: {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
