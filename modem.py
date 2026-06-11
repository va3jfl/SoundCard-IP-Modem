"""Modem core.

Owns the DSP, framing and link layers and exposes a block-oriented audio
API that works the same for a real sounddevice stream and for the
software-loopback selftest:

    out = modem.pull_tx(n)      # (n, 2) float32 to the DAC
    modem.push_rx(block)        # (n, 2) float32 from the ADC

Lane mapping:
    bond on  : lane 0 = Left, lane 1 = Right (independent frames, striped)
    bond off : lane 0 only; TX is duplicated on L and R, RX listens on L.
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from .config import ModemConfig, lane_count, FT_DATA
from .dsp import OFDM, Demod
from .framing import FrameBuilder, Header
from .link import LinkEngine


class _Lane:
    def __init__(self, idx: int, ofdm: OFDM, header_cb, frame_cb):
        self.idx = idx
        self.demod = Demod(ofdm, header_cb, frame_cb, name="LR"[idx])
        self.fifo: deque[np.ndarray] = deque()
        self.fifo_n = 0
        self.pending_hdr: Header | None = None
        self.lock = threading.Lock()

    def push_tx(self, samples: np.ndarray):
        with self.lock:
            self.fifo.append(samples)
            self.fifo_n += len(samples)

    def pop_tx(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            i = 0
            while i < n and self.fifo:
                head = self.fifo[0]
                take = min(n - i, len(head))
                out[i:i + take] = head[:take]
                if take == len(head):
                    self.fifo.popleft()
                else:
                    self.fifo[0] = head[take:]
                self.fifo_n -= take
                i += take
        return out


class Modem:
    def __init__(self, cfg: ModemConfig, log=lambda *_: None,
                 clock=time.monotonic):
        self.cfg = cfg
        self.log = log
        self.clock = clock
        self.ofdm = OFDM(cfg)
        self.builder = FrameBuilder(cfg, self.ofdm)
        self.link = LinkEngine(
            cfg, self.builder.max_payload, self._to_host, self._local_snr,
            frame_dur_fn=lambda mode: self.builder.frame_duration(cfg.max_data_syms),
            log=log, clock=clock)
        self.lanes = [
            _Lane(i, self.ofdm,
                  header_cb=self._mk_header_cb(i),
                  frame_cb=self._mk_frame_cb(i))
            for i in range(lane_count(cfg))
        ]
        self.host_send = None          # set by host interface: fn(pkt bytes)
        self._low_water = 2 * cfg.blocksize
        self._t0 = time.monotonic()
        self.audio_level_in = 0.0

    # -------------------------------------------------------- host plumbing
    def attach_host(self, send_fn):
        self.host_send = send_fn

    def host_packet_in(self, pkt: bytes):
        self.link.host_packet_in(pkt)

    def _to_host(self, pkt: bytes):
        if self.host_send:
            self.host_send(pkt)

    def _local_snr(self) -> float:
        vals = [ln.demod.metrics["snr"] for ln in self.lanes]
        return float(np.mean(vals)) if vals else 0.0

    # ----------------------------------------------------------- RX wiring
    def _mk_header_cb(self, idx: int):
        def cb(hard_bits):
            h = self.builder.parse_header_bits(hard_bits)
            if h is None or h.nsyms > self.cfg.max_data_syms:
                return None
            self.lanes[idx].pending_hdr = h
            return (h.nsyms, h.mode, h.mpx)
        return cb

    def _mk_frame_cb(self, idx: int):
        def cb(data_bits, metrics):
            h = self.lanes[idx].pending_hdr
            self.lanes[idx].pending_hdr = None
            if h is None:
                return
            now = self.clock()
            if h.nsyms == 0:
                self.link.on_frame(h, b"", idx, now)
            elif data_bits is not None:
                payload = self.builder.decode_payload(data_bits, h)
                self.link.on_frame(h, payload, idx, now)
        return cb

    # ----------------------------------------------------------- audio API
    def pull_tx(self, n: int) -> np.ndarray:
        now = self.clock()
        for ln in self.lanes:
            while ln.fifo_n < self._low_water:
                spec = self.link.next_frame(ln.idx, now)
                if spec is None:
                    break
                ftype, seq, ack, snr, payload, mode, mpx, use_fec = spec
                samples, _ = self.builder.build(ftype, seq, ack, snr,
                                                payload, mode, mpx, use_fec)
                ln.push_tx(samples)
        out = np.zeros((n, 2), dtype=np.float32)
        out[:, 0] = self.lanes[0].pop_tx(n)
        if len(self.lanes) > 1:
            out[:, 1] = self.lanes[1].pop_tx(n)
        else:
            out[:, 1] = out[:, 0]                     # duplicate mono lane
        return out

    def push_rx(self, block: np.ndarray):
        if block.ndim == 1:
            block = np.stack([block, block], axis=1)
        self.audio_level_in = float(np.sqrt(np.mean(block[:, 0] ** 2)) + 1e-12)
        self.lanes[0].demod.feed(block[:, 0])
        if len(self.lanes) > 1:
            self.lanes[1].demod.feed(block[:, 1])

    # -------------------------------------------------------------- status
    def metrics(self) -> dict:
        m = self.link.stats()
        m["lanes"] = []
        for ln in self.lanes:
            d = dict(ln.demod.metrics)
            d["sync"] = ln.demod.state != 0 or (self.clock() - m["t_rx"] < 1.5)
            m["lanes"].append(d)
        m["level_in_db"] = 20 * np.log10(self.audio_level_in + 1e-9)
        m["snr"] = self._local_snr()
        return m

    def const_points(self, lane=0, n=400):
        pts = self.lanes[lane].demod.const_points
        out = pts[-n:]
        del pts[:max(0, len(pts) - n)]
        return out
