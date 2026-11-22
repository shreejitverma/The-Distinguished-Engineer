"""RFC 6962 Merkle tree with RFC 9162 inclusion-proof verification, for a toy transparency log."""

import hashlib


def leaf_hash(data):
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left, right):
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n):
    """Largest power of two strictly smaller than n (RFC 6962 section 2.1)."""
    return 1 << ((n - 1).bit_length() - 1)


def mth(hashes):
    n = len(hashes)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return hashes[0]
    k = _split(n)
    return node_hash(mth(hashes[:k]), mth(hashes[k:]))


def audit_path(m, hashes):
    """PATH(m, D[n]) from RFC 6962 section 2.1.1, over already-computed leaf hashes."""
    n = len(hashes)
    if n == 1:
        return []
    k = _split(n)
    if m < k:
        return audit_path(m, hashes[:k]) + [mth(hashes[k:])]
    return audit_path(m - k, hashes[k:]) + [mth(hashes[:k])]


def verify_inclusion(leaf, index, size, path, root):
    """RFC 9162 section 2.1.3.2: verify an inclusion proof without the tree."""
    if index >= size:
        return False
    fn, sn = index, size - 1
    r = leaf
    for p in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root
