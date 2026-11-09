#!/usr/bin/env python3
"""Pure standard-library parser and validator for Git reftable files and stacks.

Implements the Git reftable binary format (Documentation/technical/reftable.adoc):
- Header v2 (28 bytes) and Footer v2 (72 bytes) with CRC-32 integrity validation.
- Variable-length integer (varint) decoding with 7-bit continuation bias (reftable/record.c).
- Block headers ('r' for refs, 'g' for logs, 'i' for index, 'o' for objects).
- Restart point indexing and binary search lookup over prefix-compressed ref blocks.
- Reference record decoding: tombstones (0x0), direct OIDs (0x1), peeled tags (0x2), symrefs (0x3).
- Deflated log block ('g') decoding with reverse update-index sorting.
- Multi-table stack resolution via tables.list with tombstone masking and geometric compaction analysis.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import zlib


def put_be24(val: int) -> bytes:
    """Encode 24-bit unsigned integer in big-endian order."""
    return bytes([(val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF])


def get_be24(data: bytes, offset: int = 0) -> int:
    """Decode 24-bit unsigned integer in big-endian order."""
    return (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]


def put_var_int(val: int) -> bytes:
    """Encode unsigned 64-bit integer using 7-bit payload and continuation bias."""
    buf = bytearray()
    buf.append(val & 0x7F)
    val >>= 7
    while val > 0:
        val -= 1
        buf.append(0x80 | (val & 0x7F))
        val >>= 7
    buf.reverse()
    return bytes(buf)


def get_var_int(data: bytes, offset: int) -> tuple[int, int]:
    """Decode 64-bit integer using 7-bit payload and continuation bias.

    Returns (value, next_offset).
    """
    val = data[offset] & 0x7F
    ptr = offset
    while data[ptr] & 0x80:
        ptr += 1
        val += 1
        val = (val << 7) | (data[ptr] & 0x7F)
    return val, ptr + 1


def reverse_uint64(val: int) -> bytes:
    """Reverse 64-bit update index for descending lexicographical sort."""
    return struct.pack(">Q", 0xFFFFFFFFFFFFFFFF - val)


def unreverse_uint64(b: bytes) -> int:
    """Recover 64-bit update index from reversed byte string."""
    rev = struct.unpack(">Q", b)[0]
    return 0xFFFFFFFFFFFFFFFF - rev


class FooterChecksumError(ValueError):
    """The trailing CRC-32 of the footer does not match its contents."""

    def __init__(self, calculated: int, expected: int):
        super().__init__(
            f"Footer CRC-32 mismatch: calculated {calculated:#010x}, expected {expected:#010x}"
        )
        self.calculated = calculated
        self.expected = expected


def header_size(version: int) -> int:
    """File header length: 24 bytes for v1, 28 bytes for v2 (reftable/basics.c header_size())."""
    return 24 if version == 1 else 28


def footer_size(version: int) -> int:
    """File footer length: 68 bytes for v1, 72 bytes for v2 (reftable/basics.c footer_size())."""
    return 68 if version == 1 else 72


def format_tz(minutes: int) -> str:
    """Render a signed minute offset from GMT as +HHMM or -HHMM, the way a reflog line prints it."""
    sign = "-" if minutes < 0 else "+"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}{mins:02d}"


class ReftableHeader:
    MAGIC = b"REFT"

    def __init__(
        self,
        version: int,
        block_size: int,
        min_update_index: int,
        max_update_index: int,
        hash_id: bytes = b"sha1",
    ):
        self.version = version
        self.block_size = block_size
        self.min_update_index = min_update_index
        self.max_update_index = max_update_index
        self.hash_id = hash_id

    def encode(self) -> bytes:
        if self.version == 1:
            return struct.pack(
                ">4sB3sQQ",
                self.MAGIC,
                1,
                put_be24(self.block_size),
                self.min_update_index,
                self.max_update_index,
            )
        return struct.pack(
            ">4sB3sQQ4s",
            self.MAGIC,
            2,
            put_be24(self.block_size),
            self.min_update_index,
            self.max_update_index,
            self.hash_id,
        )

    @classmethod
    def decode(cls, data: bytes) -> ReftableHeader:
        if len(data) < 24:
            raise ValueError(f"Header too short: {len(data)} bytes")
        magic = data[:4]
        if magic != cls.MAGIC:
            raise ValueError(f"Invalid magic: {magic!r}")
        version = data[4]
        block_size = get_be24(data, 5)
        min_idx, max_idx = struct.unpack(">QQ", data[8:24])
        hash_id = b"sha1"
        if version == 2:
            if len(data) < 28:
                raise ValueError("Version 2 header requires 28 bytes")
            hash_id = data[24:28]
        return cls(version, block_size, min_idx, max_idx, hash_id)


class ReftableFooter:
    def __init__(
        self,
        header: ReftableHeader,
        ref_index_pos: int = 0,
        obj_pos: int = 0,
        obj_id_len: int = 0,
        obj_index_pos: int = 0,
        log_pos: int = 0,
        log_index_pos: int = 0,
    ):
        self.header = header
        self.ref_index_pos = ref_index_pos
        self.obj_pos = obj_pos
        self.obj_id_len = obj_id_len
        self.obj_index_pos = obj_index_pos
        self.log_pos = log_pos
        self.log_index_pos = log_index_pos

    def encode(self) -> bytes:
        hdr_bytes = self.header.encode()
        body = bytearray(hdr_bytes)
        body.extend(struct.pack(">Q", self.ref_index_pos))
        body.extend(struct.pack(">Q", (self.obj_pos << 5) | (self.obj_id_len & 0x1F)))
        body.extend(struct.pack(">Q", self.obj_index_pos))
        body.extend(struct.pack(">Q", self.log_pos))
        body.extend(struct.pack(">Q", self.log_index_pos))
        crc = zlib.crc32(body)
        body.extend(struct.pack(">I", crc))
        return bytes(body)

    @classmethod
    def decode(cls, data: bytes, version: int) -> ReftableFooter:
        expected_len = footer_size(version)
        if len(data) != expected_len:
            raise ValueError(f"Footer expected {expected_len} bytes, got {len(data)}")
        expected_crc = struct.unpack(">I", data[-4:])[0]
        actual_crc = zlib.crc32(data[:-4])
        if actual_crc != expected_crc:
            raise FooterChecksumError(actual_crc, expected_crc)
        hdr_len = header_size(version)
        hdr = ReftableHeader.decode(data[:hdr_len])
        ref_idx_pos, obj_field, obj_idx_pos, log_pos, log_idx_pos = struct.unpack(
            ">QQQQQ", data[hdr_len : hdr_len + 40]
        )
        obj_pos = obj_field >> 5
        obj_id_len = obj_field & 0x1F
        return cls(
            hdr, ref_idx_pos, obj_pos, obj_id_len, obj_idx_pos, log_pos, log_idx_pos
        )


class RefRecord:
    TYPE_DELETION = 0x0
    TYPE_OID = 0x1
    TYPE_PEELED = 0x2
    TYPE_SYMREF = 0x3

    def __init__(
        self,
        name: str,
        update_index: int,
        val_type: int,
        oid: str | None = None,
        peeled: str | None = None,
        symref: str | None = None,
    ):
        self.name = name
        self.update_index = update_index
        self.val_type = val_type
        self.oid = oid
        self.peeled = peeled
        self.symref = symref

    def __repr__(self) -> str:
        if self.val_type == self.TYPE_DELETION:
            return f"RefRecord({self.name!r}, update_index={self.update_index}, DELETED)"
        if self.val_type == self.TYPE_OID:
            return f"RefRecord({self.name!r}, update_index={self.update_index}, OID={self.oid})"
        if self.val_type == self.TYPE_PEELED:
            return f"RefRecord({self.name!r}, update_index={self.update_index}, OID={self.oid}, PEELED={self.peeled})"
        if self.val_type == self.TYPE_SYMREF:
            return f"RefRecord({self.name!r}, update_index={self.update_index}, SYMREF={self.symref})"
        return f"RefRecord({self.name!r}, val_type={self.val_type})"


class LogRecord:
    def __init__(
        self,
        refname: str,
        update_index: int,
        old_id: str,
        new_id: str,
        name: str,
        email: str,
        time_seconds: int,
        tz_offset: int,
        message: str,
    ):
        self.refname = refname
        self.update_index = update_index
        self.old_id = old_id
        self.new_id = new_id
        self.name = name
        self.email = email
        self.time_seconds = time_seconds
        self.tz_offset = tz_offset
        self.message = message

    def __repr__(self) -> str:
        return (
            f"LogRecord({self.refname}@{self.update_index}: {self.old_id[:8]} -> {self.new_id[:8]} "
            f"by {self.name} <{self.email}> at {self.time_seconds} [{format_tz(self.tz_offset)}]: {self.message})"
        )


class ReftableWriter:
    """Builds valid binary reftable tables according to gitformat-reftable."""

    def __init__(
        self,
        block_size: int = 4096,
        restart_interval: int = 16,
        min_update_index: int = 1,
        max_update_index: int = 1,
        hash_id: bytes = b"sha1",
    ):
        self.block_size = block_size
        self.restart_interval = restart_interval
        self.min_update_index = min_update_index
        self.max_update_index = max_update_index
        self.hash_id = hash_id
        self.refs: list[RefRecord] = []
        self.logs: list[LogRecord] = []

    def add_ref(self, ref: RefRecord) -> None:
        self.refs.append(ref)

    def add_log(self, log: LogRecord) -> None:
        self.logs.append(log)

    def write(self) -> bytes:
        # Sort references lexicographically by byte order
        self.refs.sort(key=lambda r: r.name.encode("utf-8"))

        hdr = ReftableHeader(
            version=2,
            block_size=self.block_size,
            min_update_index=self.min_update_index,
            max_update_index=self.max_update_index,
            hash_id=self.hash_id,
        )
        hdr_bytes = hdr.encode()

        # Build Ref Block (shares the first block with the header)
        ref_block_body = bytearray()
        prev_name = ""
        restart_offsets = []
        record_count = 0

        # In the first block, record offsets start after header (28) + block header (4) = 32
        for ref in self.refs:
            curr_name = ref.name
            is_restart = (record_count % self.restart_interval) == 0
            if is_restart:
                prefix_len = 0
                suffix = curr_name.encode("utf-8")
                # restart offset relative to file start for first block
                restart_offsets.append(len(hdr_bytes) + 4 + len(ref_block_body))
            else:
                # Calculate shared prefix
                p_len = 0
                max_p = min(len(prev_name), len(curr_name))
                while p_len < max_p and prev_name[p_len] == curr_name[p_len]:
                    p_len += 1
                prefix_len = p_len
                suffix = curr_name[prefix_len:].encode("utf-8")

            ref_block_body.extend(put_var_int(prefix_len))
            ref_block_body.extend(put_var_int((len(suffix) << 3) | ref.val_type))
            ref_block_body.extend(suffix)

            update_delta = ref.update_index - self.min_update_index
            ref_block_body.extend(put_var_int(update_delta))

            if ref.val_type == RefRecord.TYPE_DELETION:
                pass
            elif ref.val_type == RefRecord.TYPE_OID:
                ref_block_body.extend(bytes.fromhex(ref.oid or "00" * 20))
            elif ref.val_type == RefRecord.TYPE_PEELED:
                ref_block_body.extend(bytes.fromhex(ref.oid or "00" * 20))
                ref_block_body.extend(bytes.fromhex(ref.peeled or "00" * 20))
            elif ref.val_type == RefRecord.TYPE_SYMREF:
                sym_bytes = (ref.symref or "").encode("utf-8")
                ref_block_body.extend(put_var_int(len(sym_bytes)))
                ref_block_body.extend(sym_bytes)

            prev_name = curr_name
            record_count += 1

        # Append restart table
        for roff in restart_offsets:
            ref_block_body.extend(put_be24(roff))
        ref_block_body.extend(struct.pack(">H", len(restart_offsets)))

        # Block length for the first block includes the 28-byte header
        first_block_total_len = len(hdr_bytes) + 4 + len(ref_block_body)
        first_block_header = b"r" + put_be24(first_block_total_len)

        out = bytearray(hdr_bytes)
        out.extend(first_block_header)
        out.extend(ref_block_body)

        # Pad to block_size if configured
        if self.block_size > 0 and len(out) < self.block_size:
            out.extend(b"\x00" * (self.block_size - len(out)))

        log_pos = 0
        if self.logs:
            # Sort logs by (refname ASC, reverse_update_index ASC)
            self.logs.sort(
                key=lambda l: (
                    l.refname.encode("utf-8")
                    + b"\x00"
                    + reverse_uint64(l.update_index)
                )
            )
            log_pos = len(out)
            log_payload = bytearray()
            prev_key = b""
            log_restarts = []
            log_cnt = 0

            for log in self.logs:
                curr_key = (
                    log.refname.encode("utf-8")
                    + b"\x00"
                    + reverse_uint64(log.update_index)
                )
                is_restart = (log_cnt % self.restart_interval) == 0
                if is_restart:
                    prefix_len = 0
                    suffix = curr_key
                    log_restarts.append(4 + len(log_payload))
                else:
                    p_len = 0
                    max_p = min(len(prev_key), len(curr_key))
                    while p_len < max_p and prev_key[p_len] == curr_key[p_len]:
                        p_len += 1
                    prefix_len = p_len
                    suffix = curr_key[prefix_len:]

                log_payload.extend(put_var_int(prefix_len))
                log_payload.extend(put_var_int((len(suffix) << 3) | 0x1))
                log_payload.extend(suffix)

                log_payload.extend(bytes.fromhex(log.old_id))
                log_payload.extend(bytes.fromhex(log.new_id))
                name_b = log.name.encode("utf-8")
                log_payload.extend(put_var_int(len(name_b)))
                log_payload.extend(name_b)
                email_b = log.email.encode("utf-8")
                log_payload.extend(put_var_int(len(email_b)))
                log_payload.extend(email_b)
                log_payload.extend(put_var_int(log.time_seconds))
                log_payload.extend(struct.pack(">h", log.tz_offset))
                msg_b = log.message.encode("utf-8")
                log_payload.extend(put_var_int(len(msg_b)))
                log_payload.extend(msg_b)

                prev_key = curr_key
                log_cnt += 1

            for lroff in log_restarts:
                log_payload.extend(put_be24(lroff))
            log_payload.extend(struct.pack(">H", len(log_restarts)))

            inflated_block_len = 4 + len(log_payload)
            compressed_data = zlib.compress(log_payload)

            log_block_header = b"g" + put_be24(inflated_block_len)
            out.extend(log_block_header)
            out.extend(compressed_data)

            if self.block_size > 0 and (len(out) % self.block_size) != 0:
                rem = self.block_size - (len(out) % self.block_size)
                out.extend(b"\x00" * rem)

        footer = ReftableFooter(
            header=hdr,
            ref_index_pos=0,
            obj_pos=0,
            obj_id_len=0,
            obj_index_pos=0,
            log_pos=log_pos,
            log_index_pos=0,
        )
        out.extend(footer.encode())
        return bytes(out)


class ReftableReader:
    """Reads and validates a binary reftable file."""

    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 24:
            raise ValueError("File too short for reftable")
        self.header = ReftableHeader.decode(data[:28])
        footer_len = footer_size(self.header.version)
        if len(data) < header_size(self.header.version) + footer_len:
            raise ValueError("File too short to contain footer")
        self.footer = ReftableFooter.decode(data[-footer_len:], self.header.version)

    def _hash_len(self) -> int:
        return 20 if self.header.hash_id == b"sha1" else 32

    def _block_layout(self, offset: int = 0) -> tuple[int, int, list[int], int]:
        """Locate a ref block: (first_record_pos, block_len, restart_offsets, restarts_start)."""
        # Only the first block is preceded by the file header, and its length counts the header.
        hdr_offset = header_size(self.header.version) if offset == 0 else 0
        blk_start = offset + hdr_offset
        blk_type = self.data[blk_start : blk_start + 1]
        if blk_type != b"r":
            raise ValueError(
                f"Expected ref block 'r', got {blk_type!r} at offset {blk_start}"
            )
        blk_len = get_be24(self.data, blk_start + 1)
        data_end = offset + blk_len
        restart_count = struct.unpack(">H", self.data[data_end - 2 : data_end])[0]
        restarts_start = data_end - 2 - (restart_count * 3)
        restart_offsets = [
            get_be24(self.data, restarts_start + i * 3) for i in range(restart_count)
        ]
        return blk_start + 4, blk_len, restart_offsets, restarts_start

    def _decode_ref_record(self, pos: int, prev_name: str) -> tuple[RefRecord, int]:
        """Decode one prefix-compressed ref record at pos; returns (record, next_pos)."""
        prefix_len, pos = get_var_int(self.data, pos)
        suffix_val, pos = get_var_int(self.data, pos)
        suffix_len = suffix_val >> 3
        val_type = suffix_val & 0x7

        suffix = self.data[pos : pos + suffix_len].decode("utf-8")
        pos += suffix_len
        curr_name = prev_name[:prefix_len] + suffix

        update_delta, pos = get_var_int(self.data, pos)
        update_idx = self.header.min_update_index + update_delta

        oid = None
        peeled = None
        symref = None
        hash_len = self._hash_len()

        if val_type == RefRecord.TYPE_DELETION:
            pass
        elif val_type == RefRecord.TYPE_OID:
            oid = self.data[pos : pos + hash_len].hex()
            pos += hash_len
        elif val_type == RefRecord.TYPE_PEELED:
            oid = self.data[pos : pos + hash_len].hex()
            pos += hash_len
            peeled = self.data[pos : pos + hash_len].hex()
            pos += hash_len
        elif val_type == RefRecord.TYPE_SYMREF:
            sym_len, pos = get_var_int(self.data, pos)
            symref = self.data[pos : pos + sym_len].decode("utf-8")
            pos += sym_len
        else:
            raise ValueError(f"Unknown value_type {val_type:#x}")

        return RefRecord(curr_name, update_idx, val_type, oid, peeled, symref), pos

    def read_ref_block(
        self, offset: int = 0
    ) -> tuple[list[RefRecord], list[int], int]:
        """Decode every record of the ref block at offset.

        Returns (records, restart_offsets, block_len).
        """
        pos, blk_len, restart_offsets, restarts_start = self._block_layout(offset)
        records: list[RefRecord] = []
        prev_name = ""
        while pos < restarts_start:
            rec, pos = self._decode_ref_record(pos, prev_name)
            records.append(rec)
            prev_name = rec.name
        return records, restart_offsets, blk_len

    def seek_ref(self, target_name: str) -> tuple[RefRecord | None, list[str]]:
        """Binary searches the restart table, then walks records from the chosen restart offset.

        Returns (record_or_none, trace_steps).
        """
        trace = []
        _, _, restart_offsets, restarts_start = self._block_layout(0)

        # Decode only the full key stored at each restart offset (prefix_len is 0 there).
        restart_keys = []
        for roff in restart_offsets:
            rec, _ = self._decode_ref_record(roff, "")
            restart_keys.append(rec.name)

        trace.append(f"Restart count: {len(restart_offsets)}")
        for idx, (roff, rkey) in enumerate(zip(restart_offsets, restart_keys)):
            trace.append(f"  restart[{idx}]: offset={roff} key={rkey!r}")

        # Binary search for the last restart key <= target.
        lo = 0
        hi = len(restart_keys) - 1
        best_restart = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = restart_keys[mid]
            trace.append(f"Binary search step: lo={lo} hi={hi} mid={mid} cand={cand!r}")
            if cand <= target_name:
                best_restart = mid
                lo = mid + 1
            else:
                hi = mid - 1

        trace.append(
            f"Bounded restart: index={best_restart} (key={restart_keys[best_restart]!r}, offset={restart_offsets[best_restart]})"
        )

        # Linear scan: decode forward from the restart offset, stopping at the next restart.
        pos = restart_offsets[best_restart]
        window_end = (
            restart_offsets[best_restart + 1]
            if best_restart + 1 < len(restart_offsets)
            else restarts_start
        )
        prev_name = ""
        while pos < window_end:
            rec, pos = self._decode_ref_record(pos, prev_name)
            prev_name = rec.name
            trace.append(f"Linear scan examine: {rec.name!r}")
            if rec.name == target_name:
                trace.append(f"Found match: {rec.name!r}")
                return rec, trace
            if rec.name > target_name:
                trace.append(f"Sorted past target: {rec.name!r} > {target_name!r}")
                break

        trace.append("Target not found")
        return None, trace

    def read_log_block(self) -> list[LogRecord]:
        """Inflates and parses log records from the log block."""
        if self.footer.log_pos == 0:
            return []
        pos = self.footer.log_pos
        blk_type = self.data[pos : pos + 1]
        if blk_type != b"g":
            raise ValueError(f"Expected log block 'g', got {blk_type!r}")
        inflated_len = get_be24(self.data, pos + 1)
        compressed_body = self.data[pos + 4 : pos + self.header.block_size]
        decomp = zlib.decompress(compressed_body)
        if len(decomp) + 4 != inflated_len:
            raise ValueError("Log block inflated length does not match its header")

        # Read restart table at end of inflated body
        restart_count = struct.unpack(">H", decomp[-2:])[0]
        restarts_start = len(decomp) - 2 - (restart_count * 3)

        logs: list[LogRecord] = []
        dpos = 0
        prev_key = b""
        hash_len = self._hash_len()

        while dpos < restarts_start:
            prefix_len, dpos = get_var_int(decomp, dpos)
            suffix_val, dpos = get_var_int(decomp, dpos)
            suffix_len = suffix_val >> 3
            log_type = suffix_val & 0x7

            suffix = decomp[dpos : dpos + suffix_len]
            dpos += suffix_len
            curr_key = prev_key[:prefix_len] + suffix

            # Parse key: refname + '\0' + reverse_uint64
            nul_idx = curr_key.find(b"\x00")
            refname = curr_key[:nul_idx].decode("utf-8")
            update_idx = unreverse_uint64(curr_key[nul_idx + 1 : nul_idx + 9])

            if log_type == 0x1:
                old_id = decomp[dpos : dpos + hash_len].hex()
                dpos += hash_len
                new_id = decomp[dpos : dpos + hash_len].hex()
                dpos += hash_len

                name_len, dpos = get_var_int(decomp, dpos)
                name = decomp[dpos : dpos + name_len].decode("utf-8")
                dpos += name_len

                email_len, dpos = get_var_int(decomp, dpos)
                email = decomp[dpos : dpos + email_len].decode("utf-8")
                dpos += email_len

                time_sec, dpos = get_var_int(decomp, dpos)
                tz_off = struct.unpack(">h", decomp[dpos : dpos + 2])[0]
                dpos += 2

                msg_len, dpos = get_var_int(decomp, dpos)
                msg = decomp[dpos : dpos + msg_len].decode("utf-8")
                dpos += msg_len

                logs.append(
                    LogRecord(
                        refname,
                        update_idx,
                        old_id,
                        new_id,
                        name,
                        email,
                        time_sec,
                        tz_off,
                        msg,
                    )
                )

            prev_key = curr_key

        return logs


TABLE_NAME_RE = re.compile(r"^0x([0-9a-f]{12})-0x([0-9a-f]{12})-[0-9a-f]{8}\.ref$")


class ReftableStack:
    """Manages a stack of reftables tracked by tables.list."""

    def __init__(self, dirpath: str):
        self.dirpath = dirpath
        self.tables: list[tuple[str, ReftableReader]] = []
        self.reload()

    def reload(self) -> None:
        list_file = os.path.join(self.dirpath, "tables.list")
        self.tables.clear()
        if not os.path.exists(list_file):
            return
        with open(list_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        for name in lines:
            match = TABLE_NAME_RE.match(name)
            if match is None:
                raise ValueError(f"Table name {name!r} is not 0x<min>-0x<max>-<random>.ref")
            path = os.path.join(self.dirpath, name)
            with open(path, "rb") as f:
                reader = ReftableReader(f.read())
            bounds = (int(match.group(1), 16), int(match.group(2), 16))
            if bounds != (reader.header.min_update_index, reader.header.max_update_index):
                raise ValueError(f"Table name {name!r} disagrees with its header update indexes")
            self.tables.append((name, reader))

    def resolve_ref(self, refname: str) -> RefRecord | None:
        """Resolve ref by scanning tables from newest to oldest."""
        for name, reader in reversed(self.tables):
            rec, _ = reader.seek_ref(refname)
            if rec is not None:
                if rec.val_type == RefRecord.TYPE_DELETION:
                    return None  # Tombstone masks older entries
                return rec
        return None

    def table_sizes(self) -> list[int]:
        """Sizes as reftable/stack.c passes them to the segment search.

        Git uses file size minus the footer (table->size) minus (header_size(version) - 1),
        where version comes from the stack's hash (1 for sha1, 2 otherwise), not each table's header.
        """
        return [
            len(reader.data)
            - footer_size(reader.header.version)
            - (header_size(1 if reader.header.hash_id == b"sha1" else 2) - 1)
            for _, reader in self.tables
        ]

    def suggest_compaction(self, factor: int = 2) -> tuple[int, int] | None:
        return suggest_compaction_segment(self.table_sizes(), factor)


def suggest_compaction_segment(sizes: list[int], factor: int = 2) -> tuple[int, int] | None:
    """Port of suggest_compaction_segment() in reftable/stack.c; (start, end) with end exclusive."""
    n = len(sizes)
    if n <= 1:
        return None

    seg_start = 0
    seg_end = 0
    bytes_acc = 0
    i = n - 1
    for i in range(n - 1, 0, -1):
        if sizes[i - 1] < sizes[i] * factor:
            seg_end = i + 1
            bytes_acc = sizes[i]
            break
    else:
        return None

    for j in range(i, 0, -1):
        curr = bytes_acc
        bytes_acc += sizes[j - 1]
        if sizes[j - 1] < curr * factor:
            seg_start = j - 1

    return (seg_start, seg_end)


def build_v1_fixture() -> bytes:
    """Assemble a one-ref SHA-1 table by hand from Documentation/technical/reftable.adoc.

    It does not use ReftableWriter, so the reader is checked against the written spec rather than
    against its own encoder. It is not a table produced by git.
    """
    name = b"refs/heads/main"
    header = b"REFT" + b"\x01" + b"\x00\x10\x00" + (1).to_bytes(8, "big") + (1).to_bytes(8, "big")
    record = (
        b"\x00"  # prefix_length = 0
        + bytes([(len(name) << 3) | 0x1])  # suffix_length << 3 | value_type 1 (one object id)
        + name
        + b"\x00"  # update_index_delta = 0
        + bytes.fromhex("5" * 40)  # 20-byte object id
    )
    restarts = (24 + 4).to_bytes(3, "big") + (1).to_bytes(2, "big")  # first record at file offset 28, one restart
    block_len = 24 + 4 + len(record) + len(restarts)  # the first block's length counts the file header
    block = b"r" + block_len.to_bytes(3, "big") + record + restarts
    footer = header + bytes(40)  # five zero uint64 section pointers: no index, object or log blocks
    footer += zlib.crc32(footer).to_bytes(4, "big")
    return header + block + footer


def run_lab_demonstration() -> None:
    print("=== REFTABLE BINARY FORMAT & STACK LAB ===")

    # 0. Varint test vectors (multi-byte values exercise the +1 continuation bias)
    print("\n--- Varint Vectors (7-bit groups, big-endian, +1 bias per continuation) ---")
    for value in (0, 127, 128, 129, 16511, 16512):
        encoded = put_var_int(value)
        decoded, nxt = get_var_int(encoded, 0)
        print(f"  {value} -> {encoded.hex()} -> {decoded} (consumed {nxt})")

    # 1. Build Table 1 (Initial transaction: update_index 1..2)
    w1 = ReftableWriter(
        block_size=4096,
        restart_interval=2,
        min_update_index=1,
        max_update_index=2,
        hash_id=b"sha1",
    )
    # Refs are added out of order on purpose: the writer must sort them by byte order.
    w1.add_ref(
        RefRecord(
            "refs/tags/v1.0.0",
            2,
            RefRecord.TYPE_PEELED,
            oid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            peeled="3333333333333333333333333333333333333333",
        )
    )
    w1.add_ref(
        RefRecord(
            "refs/heads/main",
            1,
            RefRecord.TYPE_OID,
            oid="3333333333333333333333333333333333333333",
        )
    )
    # Directory/file names coexist in the format, although Git itself rejects such a pair.
    w1.add_ref(
        RefRecord(
            "refs/heads/feature/login",
            1,
            RefRecord.TYPE_OID,
            oid="2222222222222222222222222222222222222222",
        )
    )
    w1.add_ref(
        RefRecord(
            "refs/heads/feature",
            1,
            RefRecord.TYPE_OID,
            oid="1111111111111111111111111111111111111111",
        )
    )
    w1.add_ref(
        RefRecord(
            "HEAD", 1, RefRecord.TYPE_SYMREF, symref="refs/heads/main"
        )
    )
    # Reflog entries are added oldest first; the writer must emit the newest first.
    w1.add_log(
        LogRecord(
            refname="refs/heads/main",
            update_index=1,
            old_id="0000000000000000000000000000000000000000",
            new_id="3333333333333333333333333333333333333333",
            name="Alice Engineer",
            email="alice@example.com",
            time_seconds=1700000000,
            tz_offset=-300,
            message="commit: initial production release",
        )
    )
    w1.add_log(
        LogRecord(
            refname="refs/heads/main",
            update_index=2,
            old_id="3333333333333333333333333333333333333333",
            new_id="4444444444444444444444444444444444444444",
            name="Alice Engineer",
            email="alice@example.com",
            time_seconds=1700000100,
            tz_offset=-300,
            message="commit: second release",
        )
    )
    t1_bytes = w1.write()

    # 2. Inspect Table 1
    r1 = ReftableReader(t1_bytes)
    print(f"Header: version={r1.header.version} block_size={r1.header.block_size} "
          f"min_idx={r1.header.min_update_index} max_idx={r1.header.max_update_index} "
          f"hash={r1.header.hash_id.decode('ascii')}")
    print(f"Footer: ref_idx_pos={r1.footer.ref_index_pos} log_pos={r1.footer.log_pos} "
          f"obj_pos={r1.footer.obj_pos} CRC32=VALID")

    # 3. Binary Search Trace
    print("\n--- Binary Search Trace: 'refs/heads/main' ---")
    rec, trace = r1.seek_ref("refs/heads/main")
    for t in trace:
        print(f"  {t}")
    assert rec is not None
    print(f"Result: {rec}")

    miss, miss_trace = r1.seek_ref("refs/heads/gone")
    print("\n--- Binary Search Miss: 'refs/heads/gone' ---")
    for t in miss_trace[-3:]:
        print(f"  {t}")
    print(f"Result: {miss}")

    # 4. Prefix Compression Recovery
    print("\n--- All Records in Table 1 (No D/F Conflict) ---")
    all_refs, _, _ = r1.read_ref_block(0)
    for r in all_refs:
        print(f"  {r}")

    # 5. Log Records in Table 1
    print("\n--- Reflog Records in Table 1 (Deflated Log Block) ---")
    logs = r1.read_log_block()
    for l in logs:
        print(f"  {l}")

    # 6. Build Table 2 (Second transaction: delete feature, update main to commit 4444...)
    w2 = ReftableWriter(
        block_size=4096,
        restart_interval=2,
        min_update_index=3,
        max_update_index=3,
        hash_id=b"sha1",
    )
    # Tombstone (value_type 0x0)
    w2.add_ref(RefRecord("refs/heads/feature", 3, RefRecord.TYPE_DELETION))
    # Update main
    w2.add_ref(
        RefRecord(
            "refs/heads/main",
            3,
            RefRecord.TYPE_OID,
            oid="4444444444444444444444444444444444444444",
        )
    )
    t2_bytes = w2.write()

    # 7. Stack Simulation via a private temporary directory
    tmpdir = tempfile.mkdtemp(prefix="reftable_lab_")
    try:
        # Git names tables 0x<min>-0x<max>-<random>.ref (reftable/stack.c); the last field is arbitrary here.
        t1_name = "0x000000000001-0x000000000002-1111aaaa.ref"
        t2_name = "0x000000000003-0x000000000003-2222bbbb.ref"
        with open(os.path.join(tmpdir, t1_name), "wb") as f:
            f.write(t1_bytes)
        with open(os.path.join(tmpdir, t2_name), "wb") as f:
            f.write(t2_bytes)
        with open(os.path.join(tmpdir, "tables.list"), "w", encoding="utf-8") as f:
            f.write(f"{t1_name}\n{t2_name}\n")

        stack = ReftableStack(tmpdir)
        print("\n--- Stack Resolution (tables.list with Tombstone Masking) ---")
        print(f"Active tables in stack: {len(stack.tables)}")
        for name, _ in stack.tables:
            # The harness scrubs 0x-prefixed hex runs, so print the parsed fields of the name instead.
            parts = TABLE_NAME_RE.match(name)
            assert parts is not None
            print(
                f"  table min_update_index={int(parts.group(1), 16)} "
                f"max_update_index={int(parts.group(2), 16)} suffix={name.split('-')[2]}"
            )

        q_feature = stack.resolve_ref("refs/heads/feature")
        print(f"Query 'refs/heads/feature': {q_feature} (Masked by tombstone in Table 2)")

        q_login = stack.resolve_ref("refs/heads/feature/login")
        print(f"Query 'refs/heads/feature/login': {q_login} (Preserved from Table 1)")

        q_main = stack.resolve_ref("refs/heads/main")
        print(f"Query 'refs/heads/main': {q_main} (Updated by Table 2)")

        # A stack must reject a table whose name is malformed or disagrees with its header.
        for label, bad_name in (
            ("malformed name", "bad.ref"),
            ("name that disagrees with the header", "0x000000000005-0x000000000005-3333cccc.ref"),
        ):
            with open(os.path.join(tmpdir, bad_name), "wb") as f:
                f.write(t2_bytes)
            with open(os.path.join(tmpdir, "tables.list"), "w", encoding="utf-8") as f:
                f.write(f"{t1_name}\n{bad_name}\n")
            try:
                ReftableStack(tmpdir)
            except ValueError:
                print(f"Stack rejected a {label}: True")
            else:
                raise SystemExit(f"Stack accepted a {label}")

        # 8. Geometric Compaction Evaluation
        print("\n--- Geometric Compaction Analysis ---")
        sizes = stack.table_sizes()
        print(f"Stack sizes as Git feeds them to the search (file - footer - header overhead): {sizes}")
        print(f"Compaction segment for the 2-table stack: {stack.suggest_compaction(factor=2)}")
        for example in ([64, 32, 16, 8, 4, 2, 1], [64, 32, 16, 8, 4, 3, 1], [128, 32, 16, 8, 4, 3, 1]):
            print(f"Compaction segment for sizes {example}: {suggest_compaction_segment(example, 2)}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\nTemporary stack directory removed: {not os.path.exists(tmpdir)}")

    # 9. Parse a table assembled by hand from the written spec (not by ReftableWriter)
    print("\n--- Hand-Assembled v1 Table (Spec-Derived, Not Written by git) ---")
    v1 = ReftableReader(build_v1_fixture())
    print(f"Header: version={v1.header.version} block_size={v1.header.block_size} "
          f"min_idx={v1.header.min_update_index} max_idx={v1.header.max_update_index} "
          f"hash={v1.header.hash_id.decode('ascii')}")
    v1_rec, _ = v1.seek_ref("refs/heads/main")
    print(f"Lookup 'refs/heads/main': {v1_rec}")
    v1_miss, _ = v1.seek_ref("refs/heads/zzz")
    print(f"Lookup 'refs/heads/zzz': {v1_miss}")

    # 10. Integrity Verification: Bit Flip Detection
    print("\n--- Integrity Verification: Bit-Flip Corruption Detection ---")
    for label, index in (("a footer pointer byte", -30), ("the stored CRC-32 byte", -1)):
        corrupt_bytes = bytearray(t1_bytes)
        corrupt_bytes[index] ^= 0x01
        try:
            ReftableReader(bytes(corrupt_bytes))
        except FooterChecksumError as e:
            print(f"Flip in {label}: rejected, calculated != stored is {e.calculated != e.expected}")
        else:
            raise SystemExit(f"Corruption in {label} went undetected")


if __name__ == "__main__":
    run_lab_demonstration()
