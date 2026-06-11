"""FEC and bit-level utilities.

Reed-Solomon RS(255,223) over GF(2^8), generator 0x11d (the classic
CCSDS/CD-ROM/ADSL code: corrects up to 16 byte errors per 255-byte
codeword).  Codewords are block-interleaved column-wise so a burst of
bad subcarriers / a click spreads across many codewords.

Also: CRC-16/CCITT for the frame header, an additive LFSR scrambler to
whiten the payload (avoids spectral lines / long runs), and packing
helpers.
"""
from __future__ import annotations
import numpy as np
import zlib

RS_N = 255
RS_K = 223
RS_T = (RS_N - RS_K) // 2  # 16

# ---------------------------------------------------------------- GF(2^8)
_GF_EXP = np.zeros(512, dtype=np.int64)
_GF_LOG = np.zeros(256, dtype=np.int64)


def _init_tables(prim=0x11D):
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= prim
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_tables()


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return int(_GF_EXP[_GF_LOG[a] + _GF_LOG[b]])


def gf_div(a, b):
    if a == 0:
        return 0
    if b == 0:
        raise ZeroDivisionError
    return int(_GF_EXP[(_GF_LOG[a] - _GF_LOG[b]) % 255])


def gf_pow(a, p):
    if a == 0:
        return 0
    return int(_GF_EXP[(_GF_LOG[a] * p) % 255])


def gf_inv(a):
    return int(_GF_EXP[255 - _GF_LOG[a]])


def _poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for i, pi in enumerate(p):
        if pi == 0:
            continue
        for j, qj in enumerate(q):
            if qj:
                r[i + j] ^= gf_mul(pi, qj)
    return r


def _poly_eval(p, x):
    # p[0] is highest-order coefficient
    y = p[0]
    for c in p[1:]:
        y = gf_mul(y, x) ^ c
    return y


# Generator polynomial g(x) = prod_{i=0}^{2t-1} (x - a^i)
_GEN = [1]
for _i in range(RS_N - RS_K):
    _GEN = _poly_mul(_GEN, [1, gf_pow(2, _i)])
_GEN_ARR = np.array(_GEN[1:], dtype=np.int64)  # without leading 1

# vectorized encode helper tables
_EXP = _GF_EXP
_LOG = _GF_LOG


def rs_encode_block(data: np.ndarray) -> np.ndarray:
    """Encode one RS(255,223) codeword. data: uint8[223] -> uint8[255]."""
    parity = np.zeros(RS_N - RS_K, dtype=np.int64)
    glog = _LOG[_GEN_ARR]
    gen_nz = _GEN_ARR != 0
    for b in data.astype(np.int64):
        fb = b ^ parity[0]
        parity[:-1] = parity[1:]
        parity[-1] = 0
        if fb:
            lf = _LOG[fb]
            prod = np.zeros_like(parity)
            prod[gen_nz] = _EXP[(glog[gen_nz] + lf) % 255]
            parity ^= prod
    out = np.empty(RS_N, dtype=np.uint8)
    out[:RS_K] = data
    out[RS_K:] = parity.astype(np.uint8)
    return out


def rs_decode_block(code: np.ndarray):
    """Decode one codeword. Returns (data[223], n_corrected) or (None, -1)."""
    code = code.astype(np.int64).copy()
    # Syndromes S_i = c(a^i), i = 0..2t-1
    synd = np.zeros(2 * RS_T, dtype=np.int64)
    nz = np.nonzero(code)[0]
    if len(nz):
        lc = _LOG[code[nz]]
        # position j (degree N-1-j) -> exponent (N-1-j)*i
        deg = (RS_N - 1 - nz)
        for i in range(2 * RS_T):
            s = 0
            e = (lc + deg * i) % 255
            v = _EXP[e]
            s = np.bitwise_xor.reduce(v)
            synd[i] = s
    if not synd.any():
        return code[:RS_K].astype(np.uint8), 0

    # Berlekamp-Massey: find error locator sigma(x)
    sigma = [1]
    prev = [1]
    L = 0
    m = 1
    b = 1
    for n in range(2 * RS_T):
        d = synd[n]
        for i in range(1, L + 1):
            d ^= gf_mul(sigma[i] if i < len(sigma) else 0, int(synd[n - i]))
        if d == 0:
            m += 1
        elif 2 * L <= n:
            t = sigma[:]
            coef = gf_mul(int(d), gf_inv(int(b)))
            shifted = [0] * m + prev
            ext = max(len(sigma), len(shifted))
            ns = [0] * ext
            for i in range(ext):
                a = sigma[i] if i < len(sigma) else 0
                c = gf_mul(coef, shifted[i]) if i < len(shifted) else 0
                ns[i] = a ^ c
            sigma = ns
            L = n + 1 - L
            prev = t
            b = d
            m = 1
        else:
            coef = gf_mul(int(d), gf_inv(int(b)))
            shifted = [0] * m + prev
            ext = max(len(sigma), len(shifted))
            ns = [0] * ext
            for i in range(ext):
                a = sigma[i] if i < len(sigma) else 0
                c = gf_mul(coef, shifted[i]) if i < len(shifted) else 0
                ns[i] = a ^ c
            sigma = ns
            m += 1
    nerr = L
    if nerr > RS_T:
        return None, -1

    # Chien search: roots of sigma -> error positions
    err_pos = []
    sig_rev = list(reversed(sigma))  # highest order first for _poly_eval
    for j in range(RS_N):
        # x = a^{-(N-1-j)} ... evaluate sigma at a^{-deg}
        deg = RS_N - 1 - j
        x = _EXP[(255 - (deg % 255)) % 255]
        if _poly_eval(sig_rev, int(x)) == 0:
            err_pos.append(j)
    if len(err_pos) != nerr:
        return None, -1

    # Forney: error magnitudes.  Omega(x) = [S(x) * sigma(x)] mod x^{2t}
    s_poly = [int(s) for s in synd]            # S_0 + S_1 x + ...
    omega = [0] * (2 * RS_T)
    for i, si in enumerate(s_poly):
        if si == 0:
            continue
        for k, sk in enumerate(sigma):
            if k + i < 2 * RS_T and sk:
                omega[i + k] ^= gf_mul(si, sk)
    # sigma'(x): formal derivative (odd-power terms)
    dsig = [0] * max(1, len(sigma) - 1)
    for i in range(1, len(sigma), 2):
        dsig[i - 1] = sigma[i]
    for j in err_pos:
        deg = RS_N - 1 - j
        xinv = _EXP[(255 - (deg % 255)) % 255]          # X_j^{-1}
        num = 0
        xp = 1
        for c in omega:
            if c:
                num ^= gf_mul(c, xp)
            xp = gf_mul(xp, int(xinv))
        den = 0
        xp = 1
        for c in dsig:
            if c:
                den ^= gf_mul(c, xp)
            xp = gf_mul(xp, int(xinv))
        if den == 0:
            return None, -1
        # fcr = 0 convention: Y_j = X_j * Omega(X_j^-1) / sigma'(X_j^-1)
        mag = gf_mul(gf_div(num, den), int(_EXP[deg % 255]))
        code[j] ^= mag

    # verify
    nz = np.nonzero(code)[0]
    if len(nz):
        lc = _LOG[code[nz]]
        deg = (RS_N - 1 - nz)
        for i in range(2 * RS_T):
            if np.bitwise_xor.reduce(_EXP[(lc + deg * i) % 255]) != 0:
                return None, -1
    return code[:RS_K].astype(np.uint8), nerr


# ------------------------------------------------------------ stream level
_PAD_RNG = np.random.default_rng(0xC0DE)
_PAD = _PAD_RNG.integers(0, 256, size=1 << 14, dtype=np.uint8).tobytes()

_SCR_RNG = np.random.default_rng(0x5C2A)
_SCRAMBLE = _SCR_RNG.integers(0, 256, size=1 << 16, dtype=np.uint8)


def scramble(data: bytes, offset: int = 0) -> bytes:
    a = np.frombuffer(data, dtype=np.uint8)
    idx = (np.arange(len(a)) + offset) % len(_SCRAMBLE)
    return (a ^ _SCRAMBLE[idx]).tobytes()


def n_codewords(payload_len: int) -> int:
    return max(1, (payload_len + RS_K - 1) // RS_K)


def fec_encode(payload: bytes) -> bytes:
    """payload -> interleaved RS codeword stream (n_cw * 255 bytes)."""
    ncw = n_codewords(len(payload))
    buf = bytearray(payload)
    pad = ncw * RS_K - len(buf)
    buf += _PAD[:pad]
    mat = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(ncw, RS_K)
    out = np.empty((ncw, RS_N), dtype=np.uint8)
    for i in range(ncw):
        out[i] = rs_encode_block(mat[i])
    return out.T.flatten().tobytes()  # column-wise interleave


def fec_decode(stream: bytes, payload_len: int):
    """Inverse of fec_encode. Returns (payload_bytes|None, total_corrected)."""
    ncw = n_codewords(payload_len)
    need = ncw * RS_N
    if len(stream) < need:
        return None, -1
    mat = np.frombuffer(stream[:need], dtype=np.uint8).reshape(RS_N, ncw).T
    out = np.empty((ncw, RS_K), dtype=np.uint8)
    total = 0
    for i in range(ncw):
        data, ne = rs_decode_block(mat[i])
        if data is None:
            return None, -1
        out[i] = data
        total += ne
    return out.flatten().tobytes()[:payload_len], total


def coded_len(payload_len: int, fec: bool) -> int:
    if not fec:
        return payload_len
    return n_codewords(payload_len) * RS_N


def max_payload_for_coded(coded_cap: int, fec: bool) -> int:
    """Largest payload length whose coded size fits in coded_cap bytes."""
    if not fec:
        return coded_cap
    ncw = coded_cap // RS_N
    return ncw * RS_K


# ----------------------------------------------------------------- CRC16
def crc16(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------- bits
def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    n = (len(bits) // 8) * 8
    return np.packbits(bits[:n].astype(np.uint8)).tobytes()


_PNBITS = np.random.default_rng(0xB175).integers(0, 2, size=1 << 16).astype(np.uint8)


def pn_bits(n: int, offset: int = 0) -> np.ndarray:
    idx = (np.arange(n) + offset) % len(_PNBITS)
    return _PNBITS[idx]
