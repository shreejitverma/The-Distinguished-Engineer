#!/usr/bin/env python3
"""Nix Derivation, NAR, and Store Path Inspector.

Inspects Nix .drv ATerm structures, computes input-addressed and fixed-output
store paths using Nix base32 and 20-byte XOR hash compression, serializes NAR
archives, and simulates runtime reference scanning.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# Nix custom base32 alphabet (32 characters: digits 0-9 and letters a-z excluding e, o, u, t)
NIX_BASE32_CHARS = "0123456789abcdfghijklmnpqrsvwxyz"


def nix_base32_encode(raw_bytes: bytes) -> str:
    """Encodes bytes into Nix base32 format using little-endian byte ordering."""
    num = int.from_bytes(raw_bytes, "little")
    num_chars = (len(raw_bytes) * 8 + 4) // 5
    chars = []
    for i in range(num_chars - 1, -1, -1):
        digit = (num >> (i * 5)) & 31
        chars.append(NIX_BASE32_CHARS[digit])
    return "".join(chars)


def compress_hash(digest: bytes, new_size: int = 20) -> bytes:
    """Folds a cryptographic hash into new_size bytes via byte-wise XOR (Nix compressHash)."""
    folded = bytearray(new_size)
    for i, byte_val in enumerate(digest):
        folded[i % new_size] ^= byte_val
    return bytes(folded)


def make_store_path(
    store_dir: str,
    path_type: str,
    inner_digest_hex: str,
    name: str,
    references: Optional[List[str]] = None,
) -> Tuple[str, str, str]:
    """Computes a Nix store path string from its fingerprint specification."""
    if path_type == "text":
        ref_prefix = ":".join(sorted(references)) if references else ""
        if ref_prefix:
            fp = f"text:{ref_prefix}:sha256:{inner_digest_hex}:{store_dir}:{name}"
        else:
            fp = f"text:sha256:{inner_digest_hex}:{store_dir}:{name}"
    elif path_type.startswith("output:"):
        fp = f"{path_type}:sha256:{inner_digest_hex}:{store_dir}:{name}"
    elif path_type == "source":
        fp = f"source:sha256:{inner_digest_hex}:{store_dir}:{name}"
    else:
        raise ValueError(f"Unsupported path_type: {path_type}")

    fp_digest = hashlib.sha256(fp.encode("utf-8")).digest()
    compressed = compress_hash(fp_digest, 20)
    b32_digest = nix_base32_encode(compressed)
    full_path = f"{store_dir}/{b32_digest}-{name}"
    return full_path, b32_digest, fp


def serialize_nar_string(s: bytes) -> bytes:
    """Serializes a byte string in NAR format: 8-byte little-endian length + padded data."""
    length = len(s).to_bytes(8, "little")
    pad_len = (8 - (len(s) % 8)) % 8
    return length + s + (b"\0" * pad_len)


def serialize_nar_file(contents: bytes, executable: bool = False) -> bytes:
    """Serializes a regular file into the Nix Archive (NAR) byte stream."""
    out = bytearray()
    out += serialize_nar_string(b"nix-archive-1")
    out += serialize_nar_string(b"(")
    out += serialize_nar_string(b"type")
    out += serialize_nar_string(b"regular")
    if executable:
        out += serialize_nar_string(b"executable")
        out += serialize_nar_string(b"")
    out += serialize_nar_string(b"contents")
    out += serialize_nar_string(contents)
    out += serialize_nar_string(b")")
    return bytes(out)


def serialize_nar_symlink(target: bytes) -> bytes:
    """Serializes a symlink into the Nix Archive (NAR) byte stream."""
    out = bytearray()
    out += serialize_nar_string(b"nix-archive-1")
    out += serialize_nar_string(b"(")
    out += serialize_nar_string(b"type")
    out += serialize_nar_string(b"symlink")
    out += serialize_nar_string(b"target")
    out += serialize_nar_string(target)
    out += serialize_nar_string(b")")
    return bytes(out)


def serialize_nar_directory(entries: List[Tuple[str, bytes]]) -> bytes:
    """Serializes a directory into the Nix Archive (NAR) byte stream.
    
    Directory entries are sorted lexicographically by name to guarantee determinism.
    Each entry payload is already wrapped in serialize1: str("(") + ... + str(")")
    """
    sorted_entries = sorted(entries, key=lambda x: x[0])
    out = bytearray()
    out += serialize_nar_string(b"nix-archive-1")
    out += serialize_nar_string(b"(")
    out += serialize_nar_string(b"type")
    out += serialize_nar_string(b"directory")
    for name, node_body in sorted_entries:
        out += serialize_nar_string(b"entry")
        out += serialize_nar_string(b"(")
        out += serialize_nar_string(b"name")
        out += serialize_nar_string(name.encode("utf-8"))
        out += serialize_nar_string(b"node")
        out += node_body
        out += serialize_nar_string(b")")
    out += serialize_nar_string(b")")
    return bytes(out)


class ATermParser:
    """Recursive descent tokenizer and parser for Nix .drv ATerm expressions."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_whitespace(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def peek(self) -> str:
        self.skip_whitespace()
        if self.pos < len(self.text):
            return self.text[self.pos]
        return ""

    def expect(self, char: str) -> None:
        self.skip_whitespace()
        if self.pos >= len(self.text) or self.text[self.pos] != char:
            found = self.text[self.pos] if self.pos < len(self.text) else "EOF"
            raise ValueError(f"Expected '{char}' at {self.pos}, found '{found}'")
        self.pos += 1

    def parse_string(self) -> str:
        self.skip_whitespace()
        self.expect('"')
        chars = []
        while self.pos < len(self.text):
            c = self.text[self.pos]
            self.pos += 1
            if c == '"':
                return "".join(chars)
            elif c == "\\":
                if self.pos >= len(self.text):
                    raise ValueError("Unterminated escape at EOF")
                esc = self.text[self.pos]
                self.pos += 1
                if esc == "n":
                    chars.append("\n")
                elif esc == "r":
                    chars.append("\r")
                elif esc == "t":
                    chars.append("\t")
                elif esc == "\\":
                    chars.append("\\")
                elif esc == '"':
                    chars.append('"')
                else:
                    chars.append(esc)
            else:
                chars.append(c)
        raise ValueError("Unterminated string literal")

    def parse_tuple(self) -> Tuple[Any, ...]:
        self.expect("(")
        items = []
        while self.peek() != ")":
            items.append(self.parse_value())
            if self.peek() == ",":
                self.expect(",")
        self.expect(")")
        return tuple(items)

    def parse_list(self) -> List[Any]:
        self.expect("[")
        items = []
        while self.peek() != "]":
            items.append(self.parse_value())
            if self.peek() == ",":
                self.expect(",")
        self.expect("]")
        return items

    def parse_value(self) -> Any:
        self.skip_whitespace()
        p = self.peek()
        if p == '"':
            return self.parse_string()
        elif p == "(":
            return self.parse_tuple()
        elif p == "[":
            return self.parse_list()
        elif p.isalnum() or p == "_":
            start = self.pos
            while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
                self.pos += 1
            ident = self.text[start:self.pos]
            if self.peek() == "(":
                # Constructor like Derive(...)
                args = self.parse_tuple()
                return (ident, args)
            return ident
        raise ValueError(f"Unexpected token starting with '{p}' at position {self.pos}")


def escape_aterm_string(s: str) -> str:
    """Escapes special characters in string for ATerm serialization."""
    escaped = []
    for c in s:
        if c == "\\":
            escaped.append("\\\\")
        elif c == '"':
            escaped.append('\\"')
        elif c == "\n":
            escaped.append("\\n")
        elif c == "\r":
            escaped.append("\\r")
        elif c == "\t":
            escaped.append("\\t")
        else:
            escaped.append(c)
    return '"' + "".join(escaped) + '"'


def serialize_aterm_value(val: Any) -> str:
    """Serializes a Python object into canonical Nix ATerm text."""
    if isinstance(val, str):
        return escape_aterm_string(val)
    elif isinstance(val, list):
        return "[" + ",".join(serialize_aterm_value(x) for x in val) + "]"
    elif isinstance(val, tuple):
        if len(val) == 2 and isinstance(val[0], str) and isinstance(val[1], tuple):
            # Constructor like ("Derive", (...))
            return val[0] + "(" + ",".join(serialize_aterm_value(x) for x in val[1]) + ")"
        return "(" + ",".join(serialize_aterm_value(x) for x in val) + ")"
    raise ValueError(f"Unsupported type for ATerm serialization: {type(val)}")


class Derivation:
    """Represents a parsed Nix .drv store derivation."""

    def __init__(
        self,
        outputs: List[Tuple[str, str, str, str]],
        input_drvs: List[Tuple[str, List[str]]],
        input_srcs: List[str],
        platform: str,
        builder: str,
        args: List[str],
        env: List[Tuple[str, str]],
    ) -> None:
        self.outputs = outputs
        self.input_drvs = input_drvs
        self.input_srcs = input_srcs
        self.platform = platform
        self.builder = builder
        self.args = args
        self.env = sorted(env, key=lambda x: x[0])

    @classmethod
    def from_aterm(cls, text: str) -> Derivation:
        parser = ATermParser(text.strip())
        parsed = parser.parse_value()
        if not (isinstance(parsed, tuple) and parsed[0] == "Derive"):
            raise ValueError("Root constructor is not Derive(...)")
        fields = parsed[1]
        if len(fields) != 7:
            raise ValueError(f"Derive requires 7 fields, found {len(fields)}")
        outputs, input_drvs, input_srcs, platform, builder, args, env = fields
        return cls(outputs, input_drvs, input_srcs, platform, builder, args, env)

    def to_aterm(self) -> str:
        drv_tuple = (
            "Derive",
            (
                self.outputs,
                self.input_drvs,
                self.input_srcs,
                self.platform,
                self.builder,
                self.args,
                self.env,
            ),
        )
        return serialize_aterm_value(drv_tuple)

    def is_fixed_output(self) -> bool:
        """Determines if the derivation is a Fixed-Output Derivation (FOD)."""
        env_dict = dict(self.env)
        return "outputHash" in env_dict and ("outputHashAlgo" in env_dict or ":" in env_dict["outputHash"])

    def mask_outputs_for_modulo(self) -> Derivation:
        """Returns a copy with output paths and corresponding env vars blanked for modulo hashing."""
        masked_outputs = []
        for out_id, path, algo, hash_val in self.outputs:
            masked_outputs.append((out_id, "", algo, hash_val))

        env_dict = dict(self.env)
        for out_id, _, _, _ in self.outputs:
            if out_id in env_dict:
                env_dict[out_id] = ""

        masked_env = sorted(env_dict.items(), key=lambda x: x[0])
        return Derivation(
            outputs=masked_outputs,
            input_drvs=self.input_drvs,
            input_srcs=self.input_srcs,
            platform=self.platform,
            builder=self.builder,
            args=self.args,
            env=masked_env,
        )

    def modulo_hash_hex(
        self, input_modulo: Optional[Dict[str, str]] = None, mask_outputs: bool = True
    ) -> str:
        """Returns hashDerivationModulo as hex for a non-fixed-output derivation.

        Each input .drv path is replaced by the hex modulo hash of that input
        (supplied in input_modulo), and the inputs are re-sorted by that hash,
        as Nix does, so the result does not depend on the inputs' own store paths.
        Nix masks the outputs of the derivation being built (mask_outputs=True) but
        not of its inputs: an input's hash is computed with mask_outputs=False.
        """
        masked = self.mask_outputs_for_modulo() if mask_outputs else self
        if masked.input_drvs:
            lookup = input_modulo or {}
            missing = [p for p, _ in masked.input_drvs if p not in lookup]
            if missing:
                raise ValueError(f"missing modulo hash for input drvs: {missing}")
            masked.input_drvs = sorted(
                ((lookup[p], list(outs)) for p, outs in masked.input_drvs), key=lambda x: x[0]
            )
        return hashlib.sha256(masked.to_aterm().encode("utf-8")).hexdigest()

    def compute_output_paths(
        self, store_dir: str = "/nix/store", input_modulo: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Calculates expected store paths for all declared outputs."""
        results = {}
        env_dict = dict(self.env)
        pkg_name = env_dict.get("name", "unnamed")

        if self.is_fixed_output():
            # Fixed-output derivation calculation
            out_hash = env_dict["outputHash"]
            mode = env_dict.get("outputHashMode", "flat")
            algo = env_dict.get("outputHashAlgo", "sha256")
            if algo != "sha256" or len(out_hash) != 64:
                raise ValueError("the lab supports only a 64-digit hex sha256 outputHash")

            if mode in ("recursive", "r"):
                # NAR + SHA-256 is a content-addressed "source" path.
                path_type, inner_hex = "source", out_hash
            elif mode == "flat":
                # Flat or other modes: hash the "fixed:out:" fingerprint first, then
                # use it as the inner digest of an "output:out" path.
                fixed_fp = f"fixed:out:{algo}:{out_hash}:"
                path_type = "output:out"
                inner_hex = hashlib.sha256(fixed_fp.encode("utf-8")).hexdigest()
            else:
                raise ValueError(f"unsupported outputHashMode: {mode}")

            full_path, _, _ = make_store_path(store_dir, path_type, inner_hex, pkg_name)
            results["out"] = full_path
            return results

        # Regular input-addressed derivation calculation
        inner_drv_hash = self.modulo_hash_hex(input_modulo)

        for out_id, _, _, _ in self.outputs:
            path_type = f"output:{out_id}"
            target_name = pkg_name if out_id == "out" else f"{pkg_name}-{out_id}"
            full_path, _, _ = make_store_path(store_dir, path_type, inner_drv_hash, target_name)
            results[out_id] = full_path

        return results

    def compute_drv_store_path(self, store_dir: str = "/nix/store") -> str:
        """Calculates the .drv file store path as a text object."""
        full_aterm = self.to_aterm()
        sha_hex = hashlib.sha256(full_aterm.encode("utf-8")).hexdigest()
        env_dict = dict(self.env)
        drv_name = f"{env_dict.get('name', 'unnamed')}.drv"
        
        # References for .drv are inputDrvs and inputSrcs
        refs = [p for p, _ in self.input_drvs] + list(self.input_srcs)
        full_path, _, _ = make_store_path(store_dir, "text", sha_hex, drv_name, references=refs)
        return full_path


def scan_references(artifact_bytes: bytes, candidate_paths: List[str]) -> List[str]:
    """Scans binary or text content for 32-character Nix base32 store hashes."""
    found = []
    for cand in candidate_paths:
        base_name = os.path.basename(cand)
        match = re.match(r"^([0-9a-df-np-sv-z]{32})-", base_name)
        if match:
            digest = match.group(1).encode("ascii")
            if digest in artifact_bytes:
                found.append(cand)
    return sorted(found)


def sri_sha256(raw: bytes) -> str:
    """Formats a SHA-256 digest of raw bytes as an SRI string (sha256-<base64>)."""
    return "sha256-" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def valid_sri_sha256(value: str) -> bool:
    """True if value is sha256-<base64> and decodes to exactly 32 bytes."""
    if not value.startswith("sha256-"):
        return False
    try:
        return len(base64.b64decode(value[7:], validate=True)) == 32
    except ValueError:
        return False


def inspect_flake_lock(lock_data: Dict[str, Any]) -> Dict[str, Any]:
    """Parses and audits a flake.lock (schema version 7) dependency graph.

    Returns the summary plus a list of "problems": a wrong schema version, a root
    input that names a missing node, a locked rev that is not 40 hex digits, or a
    narHash that is not a valid SRI sha256 string.
    """
    version = lock_data.get("version")
    root_key = lock_data.get("root", "root")
    nodes = lock_data.get("nodes", {})
    problems: List[str] = []
    if version != 7:
        problems.append(f"schema version {version} is not 7")

    direct_inputs = nodes.get(root_key, {}).get("inputs", {})
    for input_name, target in direct_inputs.items():
        if not isinstance(target, str) or target not in nodes:
            problems.append(f"root input {input_name} points at missing node {target!r}")
    summary_nodes = {}

    for node_name, node_val in nodes.items():
        if node_name == root_key:
            continue
        locked = node_val.get("locked", {})
        if not re.fullmatch(r"[0-9a-f]{40}", locked.get("rev", "")):
            problems.append(f"node {node_name}: rev is not a 40-digit hex commit")
        if not valid_sri_sha256(locked.get("narHash", "")):
            problems.append(f"node {node_name}: narHash is not a valid sha256 SRI string")
        summary_nodes[node_name] = {
            "type": locked.get("type", "unknown"),
            "owner": locked.get("owner", ""),
            "repo": locked.get("repo", ""),
            "rev": locked.get("rev", "")[:12] if locked.get("rev") else "none",
            "narHash": locked.get("narHash", "none"),
        }
    
    return {
        "version": version,
        "root": root_key,
        "direct_inputs": direct_inputs,
        "nodes": summary_nodes,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Nix Derivation and Store Inspector")
    parser.add_argument("--inspect-drv", type=str, help="Path to .drv file to inspect")
    parser.add_argument("--test-suite", action="store_true", help="Run deterministic inspection test suite")
    args = parser.parse_args()

    if args.test_suite:
        run_deterministic_suite()
        return 0

    if args.inspect_drv:
        with open(args.inspect_drv, "r", encoding="utf-8") as f:
            drv = Derivation.from_aterm(f.read())
        print(f"System:  {drv.platform}")
        print(f"Builder: {drv.builder}")
        print(f"Outputs: {drv.outputs}")
        paths = drv.compute_output_paths()
        for out_id, path in paths.items():
            print(f"Calculated output [{out_id}]: {path}")
        print(f"Calculated drv path: {drv.compute_drv_store_path()}")
        return 0

    parser.print_help()
    return 1


def run_deterministic_suite() -> None:
    print("=== [PHASE 1] ATerm Derivation Parsing and Modulo Hashing ===")
    sample_drv_text = (
        'Derive([("out","/nix/store/z47bp6az8rj78ngb0g3q43fcmq4qqcvp-test","","")],'
        '[],[],"aarch64-darwin","/bin/sh",[],'
        '[("builder","/bin/sh"),("name","test"),("out","/nix/store/z47bp6az8rj78ngb0g3q43fcmq4qqcvp-test"),("system","aarch64-darwin")])'
    )
    drv = Derivation.from_aterm(sample_drv_text)
    print("Parsed Derivation System:       " + drv.platform)
    print("Parsed Derivation Builder:      " + drv.builder)
    print("Declared Outputs Count:         " + str(len(drv.outputs)))
    print("Declared Input Drvs Count:      " + str(len(drv.input_drvs)))
    print("Declared Input Srcs Count:      " + str(len(drv.input_srcs)))

    masked = drv.mask_outputs_for_modulo()
    masked_text = masked.to_aterm()
    masked_hash = hashlib.sha256(masked_text.encode("utf-8")).hexdigest()
    print("Masked Derivation SHA-256:      " + masked_hash)

    print("\n=== [PHASE 2] Store Path Hashing and Nix Base32 Encoding ===")
    computed_outputs = drv.compute_output_paths()
    calc_out = computed_outputs["out"]
    print("Computed Output Path:           " + calc_out)
    expected_out = "/nix/store/z47bp6az8rj78ngb0g3q43fcmq4qqcvp-test"
    print("Expected Output Path:           " + expected_out)
    assert calc_out == expected_out, f"Mismatch: {calc_out} != {expected_out}"
    print("Output Path Derivation Check:   EXACT MATCH")

    calc_drv_path = drv.compute_drv_store_path()
    print("Computed .drv Store Path:       " + calc_drv_path)
    expected_drv = "/nix/store/v6jb8ivyqx4jwja6x1w2yk5js3074vn6-test.drv"
    print("Expected .drv Store Path:       " + expected_drv)
    assert calc_drv_path == expected_drv, f"Mismatch: {calc_drv_path} != {expected_drv}"
    print(".drv Store Path Check:          EXACT MATCH")

    # A derivation with an input drv: expected paths come from nix-instantiate on Nix 2.34.7.
    dep_text = (
        'Derive([("out","/nix/store/hnn5ikdmjnlg5appv9mg7cyn15635ps5-dep","","")],[],[],"aarch64-darwin","/bin/sh",[],'
        '[("builder","/bin/sh"),("name","dep"),("out","/nix/store/hnn5ikdmjnlg5appv9mg7cyn15635ps5-dep"),("system","aarch64-darwin")])'
    )
    dep_drv_path = "/nix/store/83g3909bz2yhy4i5fg7x71xg79sg71yg-dep.drv"
    app_text = (
        'Derive([("out","/nix/store/lfpmvd3syq3cxjxhlyi272brmp0gn1kk-app","","")],'
        '[("/nix/store/83g3909bz2yhy4i5fg7x71xg79sg71yg-dep.drv",["out"])],[],"aarch64-darwin","/bin/sh",'
        '["-c","echo /nix/store/hnn5ikdmjnlg5appv9mg7cyn15635ps5-dep"],'
        '[("builder","/bin/sh"),("dep","/nix/store/hnn5ikdmjnlg5appv9mg7cyn15635ps5-dep"),("name","app"),'
        '("out","/nix/store/lfpmvd3syq3cxjxhlyi272brmp0gn1kk-app"),("system","aarch64-darwin")])'
    )
    dep = Derivation.from_aterm(dep_text)
    app = Derivation.from_aterm(app_text)
    assert dep.compute_drv_store_path() == dep_drv_path
    dep_modulo = dep.modulo_hash_hex(mask_outputs=False)
    app_out = app.compute_output_paths(input_modulo={dep_drv_path: dep_modulo})["out"]
    app_expected = "/nix/store/lfpmvd3syq3cxjxhlyi272brmp0gn1kk-app"
    print("Input-Drv Output Path:          " + app_out)
    assert app_out == app_expected, f"Mismatch: {app_out} != {app_expected}"
    app_drv = app.compute_drv_store_path()
    print("Input-Drv .drv Store Path:      " + app_drv)
    assert app_drv == "/nix/store/ai7s1x13997sd2hn6ap7ia97qkflszcj-app.drv"
    print("Input-Drv Path Checks:          EXACT MATCH")

    print("\n=== [PHASE 3] Fixed-Output Derivation (FOD) Decoupling ===")
    fod_drv_text = (
        'Derive([("out","/nix/store/svgymjq6pwxrc6ajihgxr6lya62wwkgv-fixed-test","r:sha256","0000000000000000000000000000000000000000000000000000000000000000")],'
        '[],[],"aarch64-darwin","/bin/sh",[],'
        '[("builder","/bin/sh"),("name","fixed-test"),("out","/nix/store/svgymjq6pwxrc6ajihgxr6lya62wwkgv-fixed-test"),'
        '("outputHash","0000000000000000000000000000000000000000000000000000000000000000"),'
        '("outputHashAlgo","sha256"),("outputHashMode","recursive"),("system","aarch64-darwin")])'
    )
    fod = Derivation.from_aterm(fod_drv_text)
    print("Is Fixed Output Derivation:     " + str(fod.is_fixed_output()))
    fod_computed = fod.compute_output_paths()
    print("Computed FOD Output Path:       " + fod_computed["out"])
    expected_fod = "/nix/store/svgymjq6pwxrc6ajihgxr6lya62wwkgv-fixed-test"
    print("Expected FOD Output Path:       " + expected_fod)
    assert fod_computed["out"] == expected_fod
    print("FOD Output Path Check:          EXACT MATCH")

    zero_hash = "0" * 64

    def make_fod(mode: str, builder: str, system: str, out_hash: str = zero_hash) -> Derivation:
        return Derivation(
            outputs=fod.outputs,
            input_drvs=fod.input_drvs,
            input_srcs=fod.input_srcs,
            platform=system,
            builder=builder,
            args=[],
            env=[
                ("builder", builder),
                ("name", "fixed-test"),
                ("outputHash", out_hash),
                ("outputHashAlgo", "sha256"),
                ("outputHashMode", mode),
                ("system", system),
            ],
        )

    # Altered builder and platform: the FOD path must not move.
    fod_altered_out = make_fod("recursive", "/usr/bin/curl", "x86_64-linux").compute_output_paths()["out"]
    print("Altered FOD Output Path:        " + fod_altered_out)
    assert fod_altered_out == expected_fod
    print("FOD Platform & Builder Invariance: CONFIRMED")

    # Control: a regular derivation's path does depend on the builder, so the check above is not vacuous.
    regular_altered = Derivation(
        outputs=drv.outputs, input_drvs=[], input_srcs=[], platform=drv.platform,
        builder="/usr/bin/curl", args=[],
        env=[("builder", "/usr/bin/curl"), ("name", "test"), ("out", drv.outputs[0][1]), ("system", drv.platform)],
    )
    regular_altered_out = regular_altered.compute_output_paths()["out"]
    assert regular_altered_out != expected_out
    print("Control (regular drv, builder changed): path changes: " + str(regular_altered_out != expected_out))

    # Flat mode (fetchurl style) uses the fixed:out: fingerprint; value from real Nix 2.34.7.
    expected_flat = "/nix/store/v96xy7krc03131l1gl7rbb197pp6gcpn-fixed-test"
    flat_out = make_fod("flat", "/bin/sh", "aarch64-darwin").compute_output_paths()["out"]
    print("Computed Flat FOD Output Path:  " + flat_out)
    print("Expected Flat FOD Output Path:  " + expected_flat)
    assert flat_out == expected_flat, f"Mismatch: {flat_out} != {expected_flat}"
    assert flat_out != expected_fod
    print("Flat FOD Path Check:            EXACT MATCH (differs from recursive)")

    other_hash_out = make_fod("recursive", "/bin/sh", "aarch64-darwin", "1" * 64).compute_output_paths()["out"]
    assert other_hash_out != expected_fod
    print("Different outputHash:           path changes: " + str(other_hash_out != expected_fod))

    print("\n=== [PHASE 4] Nix Archive (NAR) Serialization and Determinism ===")
    # Expected sizes and digests come from `nix-store --dump` on Nix 2.34.7 for the same inputs.
    file_content = b"fn main() { println!(\"Hello from Nix Store!\"); }\n"
    nar_regular = serialize_nar_file(file_content, executable=False)
    print("Regular File NAR Size:          " + str(len(nar_regular)) + " bytes")
    assert len(nar_regular) == 168
    assert hashlib.sha256(nar_regular).hexdigest() == "43356907fdc99d4a6ffdb2bdeba827a3dad1910929f391a059e06548f2163cce"
    nar_exec = serialize_nar_file(file_content, executable=True)
    print("Executable File NAR Size:       " + str(len(nar_exec)) + " bytes")
    assert len(nar_exec) == 200
    assert hashlib.sha256(nar_exec).hexdigest() == "ef5fa25df6ce232a30d28e242fc7339e5b4b537b383e2554543ae395935b56b3"
    nar_link = serialize_nar_symlink(b"target/path")
    print("Symlink NAR Size:               " + str(len(nar_link)) + " bytes")
    assert len(nar_link) == 128
    assert hashlib.sha256(nar_link).hexdigest() == "8f9647d2482f5ba0ef2d9819728a26446a1b6ac85d54e2b0b808b7fbea96ac4c"
    lengths_aligned = all(len(n) % 8 == 0 for n in (nar_regular, nar_exec, nar_link))
    print("All NAR Lengths Multiple of 8:  " + str(lengths_aligned))
    assert lengths_aligned
    assert serialize_nar_string(b"abc") == b"\x03" + b"\0" * 7 + b"abc" + b"\0" * 5

    empty_dir = (
        serialize_nar_string(b"(") + serialize_nar_string(b"type")
        + serialize_nar_string(b"directory") + serialize_nar_string(b")")
    )
    dir_entries = [("bin", empty_dir), ("lib", empty_dir)]
    nar_dir = serialize_nar_directory(dir_entries)
    nar_hash = hashlib.sha256(nar_dir).hexdigest()
    print("Directory NAR Digest (SHA-256): " + nar_hash)
    assert nar_hash == "36fa233b9c286a39a7d996133c8de5fd8e9cf4997700a505b84831b8aaa19985"
    reversed_hash = hashlib.sha256(serialize_nar_directory(list(reversed(dir_entries)))).hexdigest()
    print("Entry Order Independent:        " + str(reversed_hash == nar_hash))
    assert reversed_hash == nar_hash

    print("\n=== [PHASE 5] Reference Scanning and Runtime Closure Pruning ===")
    dep_glibc = "/nix/store/a4b8gp9mw70sn9s8igmjfwmnfi62jsxy-glibc-2.39"
    dep_gcc = "/nix/store/v6jb8ivyqx4jwja6x1w2yk5js3074vn6-gcc-13.2.0"
    dep_openssl = "/nix/store/z47bp6az8rj78ngb0g3q43fcmq4qqcvp-openssl-3.0.13"

    all_build_inputs = [dep_glibc, dep_gcc, dep_openssl]
    print("Build-Time Inputs (inputDrvs):  " + str(len(all_build_inputs)))

    # Simulated ELF binary compiled with rpath to glibc and openssl, but gcc stripped
    simulated_elf = (
        b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x02\x00\xb7\x00\x01\x00\x00\x00"
        b"/nix/store/a4b8gp9mw70sn9s8igmjfwmnfi62jsxy-glibc-2.39/lib/libc.so.6\0"
        b"/nix/store/z47bp6az8rj78ngb0g3q43fcmq4qqcvp-openssl-3.0.13/lib/libssl.so.3\0"
        b"Compiled binary payload without compiler string context."
    )
    scanned_refs = scan_references(simulated_elf, all_build_inputs)
    print("Runtime References Found:       " + str(len(scanned_refs)))
    for r in scanned_refs:
        print("  Retained Runtime Ref:         " + r)
    assert dep_glibc in scanned_refs
    assert dep_openssl in scanned_refs
    assert dep_gcc not in scanned_refs
    assert scan_references(simulated_elf, [dep_glibc.replace("a4b8gp9m", "a4b8gp9n")]) == []
    assert scan_references(b"", all_build_inputs) == []
    truncated = b"/nix/store/" + dep_glibc.split("/")[-1].encode("ascii")[:31] + b"x/lib/libc.so.6\0"
    assert scan_references(truncated, [dep_glibc]) == []
    print("Truncated Hash Not Matched:     True")
    print("Compiler Pruning Verification:  PASS (gcc pruned from runtime closure)")

    print("\n=== [PHASE 6] Flake Lock Graph and Input Pinning Audit ===")
    sample_lock = {
        "version": 7,
        "root": "root",
        "nodes": {
            "root": {
                "inputs": {
                    "nixpkgs": "nixpkgs",
                    "flake-utils": "flake-utils"
                }
            },
            "nixpkgs": {
                "inputs": {},
                "locked": {
                    "type": "github",
                    "owner": "NixOS",
                    "repo": "nixpkgs",
                    "rev": "77850a98f5b9d5c4864eb8ec3da30a7d975a59a7",
                    "narHash": sri_sha256(nar_regular)
                },
                "original": {
                    "type": "github",
                    "owner": "NixOS",
                    "repo": "nixpkgs",
                    "ref": "nixos-unstable"
                }
            },
            "flake-utils": {
                "inputs": {},
                "locked": {
                    "type": "github",
                    "owner": "numtide",
                    "repo": "flake-utils",
                    "rev": "b1d9ab7cd6ff377011f0274fbe89a59c4a755d9a",
                    "narHash": sri_sha256(nar_dir)
                },
                "original": {
                    "type": "github",
                    "owner": "numtide",
                    "repo": "flake-utils"
                }
            }
        }
    }
    audit = inspect_flake_lock(sample_lock)
    print("Flake Lock Schema Version:      " + str(audit["version"]))
    print("Flake Root Direct Inputs:       " + ", ".join(sorted(audit["direct_inputs"].keys())))
    for k, v in sorted(audit["nodes"].items()):
        print(f"Node [{k}]: rev={v['rev']} type={v['type']}")
    print("Flake Lock Problems Found:      " + str(len(audit["problems"])))
    assert audit["problems"] == [], audit["problems"]
    assert audit["version"] == 7

    # narHash is the SRI SHA-256 of a NAR; this value equals `nix hash path` of a dir holding empty bin/ and lib/.
    assert sri_sha256(nar_dir) == "sha256-NvojO5woajmn2ZYTPI3l/Y6c9Jl3AKUFuEgxuKqhmYU="
    print("narHash of Directory NAR:       " + sri_sha256(nar_dir))

    # Negative case: a corrupted lock must be flagged, so a PASS above means something.
    bad_lock = json.loads(json.dumps(sample_lock))
    bad_lock["version"] = 6
    bad_lock["nodes"]["root"]["inputs"]["ghost"] = "missing"
    del bad_lock["nodes"]["nixpkgs"]["locked"]["rev"]
    bad_lock["nodes"]["flake-utils"]["locked"]["narHash"] = "sha256-tooshort="
    bad_problems = inspect_flake_lock(bad_lock)["problems"]
    print("Corrupted Lock Problems Found:  " + str(len(bad_problems)))
    assert len(bad_problems) == 4, bad_problems
    assert not valid_sri_sha256("sha256-" + base64.b64encode(b"x" * 16).decode("ascii"))
    assert not valid_sri_sha256("sha1-" + base64.b64encode(b"x" * 32).decode("ascii"))
    assert valid_sri_sha256(sri_sha256(b"x"))
    print("Flake Lock Audit:               PASS")


if __name__ == "__main__":
    sys.exit(main())
