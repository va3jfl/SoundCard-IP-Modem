"""Frame building and parsing.

Frame on the wire (per lane):

  [preamble A][preamble B][header symbol (QPSK)][data symbols ...][guard]

Header (12 bytes, repeated 3x bit-wise, majority vote at RX, padded
with PN bits to fill the header symbol):

  0   magic        0xA7
  1   ver_type     (VERSION << 4) | frame_type
  2   flags        bit0..2 mode_id, bit3 mpx, bit4 fec
  3   nsyms        number of data symbols following (0 for ctl frames)
  4   seq          go-back-N sequence number (mod 256)
  5   ack          cumulative ack: next sequence number expected
  6,7 length       payload bytes before FEC (incl. trailing CRC32), BE
  8   snr_q        local measured SNR report, dB * 4, clipped to 255
  9   reserved     0
  10,11 crc16      CCITT over bytes 0..9

Payload pipeline (TX):  raw -> +CRC32 -> scramble -> [RS encode] -> bits
              (RX):  bits -> [RS decode] -> descramble -> CRC32 check
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .config import (ModemConfig, MAGIC, VERSION, FT_DATA, MODE_ID, ID_MODE,
                     MODE_BITS)
from .dsp import OFDM
from . import fec as F

HDR_LEN = 12
HDR_BITS = HDR_LEN * 8          # 96
HDR_REP = 3                     # 288 bits on air


@dataclass
class Header:
    ftype: int
    flags: int
    nsyms: int
    seq: int
    ack: int
    length: int
    snr_q: int

    @property
    def mode(self) -> str:
        return ID_MODE.get(self.flags & 0x07, "qpsk")

    @property
    def mpx(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def fec(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def snr_db(self) -> float:
        return self.snr_q / 4.0


def pack_header(h: Header) -> bytes:
    b = struct.pack(">BBBBBBHBB",
                    MAGIC, (VERSION << 4) | (h.ftype & 0x0F), h.flags & 0xFF,
                    h.nsyms & 0xFF, h.seq & 0xFF, h.ack & 0xFF,
                    h.length & 0xFFFF, h.snr_q & 0xFF, 0)
    return b + struct.pack(">H", F.crc16(b))


def unpack_header(b: bytes) -> Header | None:
    if len(b) != HDR_LEN or b[0] != MAGIC:
        return None
    (crc,) = struct.unpack(">H", b[10:12])
    if F.crc16(b[:10]) != crc:
        return None
    ver_type = b[1]
    if (ver_type >> 4) != VERSION:
        return None
    return Header(ftype=ver_type & 0x0F, flags=b[2], nsyms=b[3],
                  seq=b[4], ack=b[5],
                  length=struct.unpack(">H", b[6:8])[0], snr_q=b[8])


def make_flags(mode: str, mpx: bool, use_fec: bool) -> int:
    return (MODE_ID[mode] & 0x07) | (0x08 if mpx else 0) | (0x10 if use_fec else 0)


def snr_quant(snr_db: float) -> int:
    return int(np.clip(round(snr_db * 4), 0, 255))


class FrameBuilder:
    """Builds modulated frames and decodes received bit streams."""

    def __init__(self, cfg: ModemConfig, ofdm: OFDM):
        self.cfg = cfg
        self.o = ofdm
        self.hdr_sym_bits = self.o.n_data * 2          # QPSK header symbol
        if HDR_BITS * HDR_REP > self.hdr_sym_bits:
            raise ValueError("header does not fit in one QPSK symbol")
        self._hdr_pad = F.pn_bits(self.hdr_sym_bits - HDR_BITS * HDR_REP,
                                  offset=911)

    # ----------------------------------------------------------- capacity
    def max_payload(self, mode: str, use_fec: bool) -> int:
        """Largest raw payload (excl. CRC32) for one frame at this mode."""
        bits_cap = self.cfg.max_data_syms * self.o.bits_per_data_sym(mode)
        coded_cap = bits_cap // 8
        raw = F.max_payload_for_coded(coded_cap, use_fec)
        return max(raw - 4, 0)                          # room for CRC32

    def nsyms_for(self, blob_len: int, mode: str, use_fec: bool) -> int:
        coded = F.coded_len(blob_len, use_fec)
        bps = self.o.bits_per_data_sym(mode)
        return (coded * 8 + bps - 1) // bps

    # ------------------------------------------------------------- build
    def _header_bits(self, h: Header) -> np.ndarray:
        bits = F.bytes_to_bits(pack_header(h))
        rep = np.tile(bits, HDR_REP)
        return np.concatenate([rep, self._hdr_pad])

    def build(self, ftype: int, seq: int, ack: int, snr_db: float,
              payload: bytes, mode: str, mpx: bool, use_fec: bool
              ) -> tuple[np.ndarray, Header]:
        """Returns (float32 samples, header). payload may be b'' (ctl)."""
        if payload:
            blob = payload + struct.pack(">I", F.crc32(payload))
            blob = F.scramble(blob)
            coded = F.fec_encode(blob) if use_fec else blob
            nsyms = self.nsyms_for(len(blob), mode, use_fec)
            bits = F.bytes_to_bits(coded)
            cap = nsyms * self.o.bits_per_data_sym(mode)
            if len(bits) < cap:
                bits = np.concatenate([bits, F.pn_bits(cap - len(bits))])
            length = len(blob)
        else:
            bits, nsyms, length = None, 0, 0

        h = Header(ftype=ftype, flags=make_flags(mode, mpx, use_fec),
                   nsyms=nsyms, seq=seq, ack=ack, length=length,
                   snr_q=snr_quant(snr_db))
        samples = self.o.mod_frame(self._header_bits(h), bits, mode, mpx)
        return samples, h

    # ------------------------------------------------------------- parse
    def parse_header_bits(self, hard_bits: np.ndarray) -> Header | None:
        """Majority-vote the 3 repetitions, verify CRC16."""
        if len(hard_bits) < HDR_BITS * HDR_REP:
            return None
        r = hard_bits[:HDR_BITS * HDR_REP].reshape(HDR_REP, HDR_BITS)
        bits = (r.sum(axis=0) >= 2).astype(np.uint8)
        return unpack_header(F.bits_to_bytes(bits))

    def decode_payload(self, data_bits: np.ndarray, h: Header) -> bytes | None:
        """Returns raw payload (CRC32 verified) or None."""
        if h.length < 5:
            return None
        coded_need = F.coded_len(h.length, h.fec)
        if len(data_bits) < coded_need * 8:
            return None
        coded = F.bits_to_bytes(data_bits[:coded_need * 8])
        if h.fec:
            blob, _ = F.fec_decode(coded, h.length)
            if blob is None:
                return None
        else:
            blob = coded[:h.length]
        blob = F.scramble(blob)                          # XOR is its own inverse
        payload, crc = blob[:-4], struct.unpack(">I", blob[-4:])[0]
        if F.crc32(payload) != crc:
            return None
        return payload

    def frame_duration(self, nsyms: int) -> float:
        return (self.o.frame_samples(nsyms) + self.cfg.guard_samples) / self.cfg.fs
