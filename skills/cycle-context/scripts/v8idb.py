"""Minimal V8 ValueSerializer (structured clone) reader — stdlib only."""
import struct

class V8Reader:
    def __init__(self, data):
        self.d = data; self.i = 0; self.objs = []
    def byte(self):
        b = self.d[self.i]; self.i += 1; return b
    def varint(self):
        r = s = 0
        while True:
            b = self.byte(); r |= (b & 0x7f) << s; s += 7
            if not b & 0x80: return r
    def zigzag(self):
        v = self.varint(); return (v >> 1) ^ -(v & 1)
    def double(self):
        v = struct.unpack_from('<d', self.d, self.i)[0]; self.i += 8; return v
    def read_header(self):
        ver = None
        while self.i < len(self.d):
            b = self.d[self.i]
            if b == 0xff:
                self.i += 1; ver = self.varint()
            elif b == 0xfe:            # trailer offset tag (v21+): 1 + 8 + 4 bytes
                self.i += 13
            else:
                break
        return ver
    def value(self):
        t = self.byte()
        if t == 0x00: return self.value()            # padding
        if t == ord('"'):                            # one-byte string
            n = self.varint(); s = self.d[self.i:self.i+n].decode('latin-1'); self.i += n; return s
        if t == ord('c'):                            # two-byte string
            n = self.varint(); s = self.d[self.i:self.i+n].decode('utf-16-le', 'replace'); self.i += n; return s
        if t == ord('S'):
            n = self.varint(); s = self.d[self.i:self.i+n].decode('utf-8', 'replace'); self.i += n; return s
        if t == ord('I'): return self.zigzag()
        if t == ord('U'): return self.varint()
        if t == ord('N'): return self.double()
        if t == ord('D'): return self.double()       # date
        if t == ord('T'): return True
        if t == ord('F'): return False
        if t == ord('0'): return None
        if t == ord('_'): return None                # undefined
        if t == ord('^'): return self.objs[self.varint()]
        if t == ord('o'):
            o = {}; self.objs.append(o)
            while self.d[self.i] != ord('{'):
                k = self.value(); v = self.value(); o[k] = v
            self.i += 1; self.varint(); return o
        if t == ord('A'):                            # dense array
            n = self.varint(); a = []; self.objs.append(a)
            for _ in range(n): a.append(self.value())
            while self.d[self.i] != ord('$'):        # extra props
                k = self.value(); v = self.value()
            self.i += 1; self.varint(); self.varint(); return a
        if t == ord('a'):                            # sparse array
            n = self.varint(); a = [None]*n; self.objs.append(a)
            while self.d[self.i] != ord('@'):
                k = self.value(); v = self.value()
                if isinstance(k, int) and 0 <= k < n: a[k] = v
            self.i += 1; self.varint(); self.varint(); return a
        if t == ord('y'):                            # boolean object true / others
            return True
        if t == ord('Z'):                            # bigint
            bl = self.varint(); self.i += bl >> 1; return 0
        if t == ord('R'):                            # regexp
            p = self.value(); self.varint(); return p
        if t == ord(';'):                            # map
            m = {}; self.objs.append(m)
            while self.d[self.i] != ord(':'):
                k = self.value(); v = self.value(); m[str(k)] = v
            self.i += 1; self.varint(); return m
        if t == ord("'"):                            # set
            s = []; self.objs.append(s)
            while self.d[self.i] != ord(','):
                s.append(self.value())
            self.i += 1; self.varint(); return s
        raise ValueError(f"unknown tag {chr(t)!r} (0x{t:02x}) at {self.i-1}")

def load(data):
    r = V8Reader(data); r.read_header(); return r.value()


def snappy_decompress(data):
    """Raw Snappy block decompression (stdlib only)."""
    i = 0; n = 0; s = 0
    while True:                       # uncompressed length varint
        b = data[i]; i += 1; n |= (b & 0x7f) << s; s += 7
        if not b & 0x80: break
    out = bytearray()
    while i < len(data):
        tag = data[i]; i += 1
        kind = tag & 3
        if kind == 0:                 # literal
            ln = tag >> 2
            if ln >= 60:
                nb = ln - 59; ln = int.from_bytes(data[i:i+nb], 'little'); i += nb
            ln += 1
            out += data[i:i+ln]; i += ln
        else:
            if kind == 1:
                ln = ((tag >> 2) & 7) + 4; off = ((tag >> 5) << 8) | data[i]; i += 1
            elif kind == 2:
                ln = (tag >> 2) + 1; off = int.from_bytes(data[i:i+2], 'little'); i += 2
            else:
                ln = (tag >> 2) + 1; off = int.from_bytes(data[i:i+4], 'little'); i += 4
            start = len(out) - off
            for k in range(ln):       # may overlap
                out.append(out[start + k])
    return bytes(out)


def load_idb_blob(data):
    """Chromium IndexedDB value: optional 0xFF 0x11 0x02 => snappy-compressed SSV."""
    if data[:3] == b'\xff\x11\x02':
        data = snappy_decompress(data[3:])
    return load(data)
