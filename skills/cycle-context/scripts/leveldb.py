"""Minimal LevelDB reader (stdlib only): the key/value pairs of a database
directory from its .ldb tables and .log write-ahead logs.

Chromium's IndexedDB stores small values inline in LevelDB (bigger ones in the
sibling *.indexeddb.blob directory). This reader is enough to enumerate them:
it does not decode IndexedDB keys, it only returns the raw user keys with
their latest value (or None when the latest record is a deletion).
"""
from pathlib import Path

try:
    from v8idb import snappy_decompress
except ImportError:  # pragma: no cover
    snappy_decompress = None

LOG_BLOCK = 32768
TABLE_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"


def _varint(data, i):
    r = s = 0
    while True:
        b = data[i]; i += 1
        r |= (b & 0x7f) << s; s += 7
        if not b & 0x80:
            return r, i


def iter_log(path):
    """Write-ahead log: yields (sequence, key, value|None) per batch entry."""
    data = Path(path).read_bytes()
    pos, record = 0, bytearray()
    while pos + 7 <= len(data):
        if LOG_BLOCK - (pos % LOG_BLOCK) < 7:        # trailer: skip to next block
            pos += LOG_BLOCK - (pos % LOG_BLOCK); continue
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        rtype = data[pos + 6]
        pos += 7
        chunk = data[pos:pos + length]; pos += length
        if rtype == 0:                                # zero type / padding
            continue
        if rtype in (1, 2):                           # FULL / FIRST
            record = bytearray(chunk)
        else:                                         # MIDDLE / LAST
            record += chunk
        if rtype in (1, 4) and len(record) >= 12:
            yield from _iter_batch(bytes(record))
            record = bytearray()


def _iter_batch(rec):
    seq = int.from_bytes(rec[:8], "little")
    count = int.from_bytes(rec[8:12], "little")
    i = 12
    for n in range(count):
        if i >= len(rec):
            return
        t = rec[i]; i += 1
        klen, i = _varint(rec, i); key = rec[i:i + klen]; i += klen
        if t == 1:
            vlen, i = _varint(rec, i); value = rec[i:i + vlen]; i += vlen
            yield seq + n, key, bytes(value)
        else:
            yield seq + n, key, None


def _block(data, offset, size):
    raw = data[offset:offset + size]
    ctype = data[offset + size] if offset + size < len(data) else 0
    if ctype == 1 and snappy_decompress:
        raw = snappy_decompress(raw)
    return raw


def _block_entries(block):
    n_restarts = int.from_bytes(block[-4:], "little")
    end = len(block) - 4 - 4 * n_restarts
    i, key = 0, b""
    while i < end:
        shared, i = _varint(block, i)
        non_shared, i = _varint(block, i)
        vlen, i = _varint(block, i)
        key = key[:shared] + block[i:i + non_shared]; i += non_shared
        value = block[i:i + vlen]; i += vlen
        yield key, value


def iter_table(path):
    """Sorted table (.ldb): yields (sequence, user key, value|None)."""
    data = Path(path).read_bytes()
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        return
    footer = data[-48:]
    i = 0
    _, i = _varint(footer, i); _, i = _varint(footer, i)     # metaindex handle
    idx_off, i = _varint(footer, i); idx_size, i = _varint(footer, i)
    for _, handle in _block_entries(_block(data, idx_off, idx_size)):
        off, j = _varint(handle, 0); size, _ = _varint(handle, j)
        for ikey, value in _block_entries(_block(data, off, size)):
            if len(ikey) < 8:
                continue
            trailer = int.from_bytes(ikey[-8:], "little")
            seq, vtype = trailer >> 8, trailer & 0xff
            yield seq, ikey[:-8], (bytes(value) if vtype == 1 else None)


def latest_values(db_dir):
    """{user key: value} with the newest version of every key (deletions
    removed) across all tables and logs of a LevelDB directory."""
    db_dir = Path(db_dir)
    best = {}
    files = sorted(db_dir.glob("*.ldb"), key=lambda p: p.stem) + sorted(db_dir.glob("*.log"), key=lambda p: p.stem)
    for f in files:
        try:
            it = iter_table(f) if f.suffix == ".ldb" else iter_log(f)
            for seq, key, value in it:
                if key not in best or seq >= best[key][0]:
                    best[key] = (seq, value)
        except Exception:
            continue  # a damaged file must not hide the others
    return {k: v for k, (s, v) in best.items() if v is not None}
