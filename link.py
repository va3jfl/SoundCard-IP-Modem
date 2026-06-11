"""Link layer: reliable in-order packet delivery over the OFDM frames.

* PDU framing inside a frame payload:  [u16 len][u8 flags][bytes]
  flags bit0 = MORE (packet continues in the next PDU, possibly next frame).
  Several small host packets are aggregated into one frame; large packets
  are fragmented.  Delivery is strictly in order (go-back-N), so
  reassembly is a simple append-until-MORE-clear buffer.

* ARQ: go-back-N, window `cfg.window`, sequence numbers mod 256.
  Every frame header carries a cumulative ACK (= next expected seq).
  ACK-only control frames are sent after `ack_delay` if nothing
  piggybacks; beacons keep the link alive when idle.

* Auto-rate: the peer reports the SNR *it* measures in every header,
  which is the quality of *our* transmit path - exactly what we need to
  pick our TX constellation.  Manual modes pin it.

Thread safety: all public methods take an internal lock; they are called
from the audio thread (next_frame / on_frame), host-interface threads
(host_packet_in) and the UI thread (stats).
"""
from __future__ import annotations

import struct
import threading
import time
from collections import deque

from .config import (ModemConfig, FT_DATA, FT_ACK, FT_BEACON, FT_PROBE,
                     MODES, AUTO_UP, AUTO_DOWN)

PDU_HDR = 3
MORE = 0x01
REASM_MAX = 4096
OUTQ_MAX_PKTS = 64
OUTQ_MAX_BYTES = 65536


def _delta(a: int, b: int) -> int:
    return (a - b) & 0xFF


class LinkEngine:
    def __init__(self, cfg: ModemConfig, max_payload_fn, send_host_packet,
                 local_snr_fn, frame_dur_fn=lambda mode: 0.34,
                 log=lambda *_: None, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.frame_dur = frame_dur_fn      # (mode) -> seconds, full frame
        self.max_payload = max_payload_fn      # (mode, fec) -> bytes
        self.send_host = send_host_packet
        self.local_snr = local_snr_fn          # () -> measured RX SNR dB
        self.log = log
        self._lock = threading.RLock()

        # TX state
        self.srtt = None            # smoothed ack RTT (adaptive RTO)
        self._dup_acks = 0          # duplicate cumulative ACKs (fast retx)
        self._fast_armed = True     # one fast-retx per ack-advance
        self.n_fast = 0             # fast-retx events (diagnostics)
        self.n_timeout = 0          # RTO-retx events (diagnostics)
        self._dup_t = 0.0           # time-gate: bonded lanes emit frame
                                    # pairs with identical acks; dups inside
                                    # half a frame are one observation
        self._retxed: set[int] = set()
        self.va = 0                 # oldest unacked
        self.vs = 0                 # next new seq
        self.tx_store: dict[int, bytes] = {}
        self.tx_sent: dict[int, float] = {}
        self.resend: deque[int] = deque()
        self.out_q: deque[tuple[float, bytes]] = deque()
        self.out_bytes = 0
        self._frag_rest = b""

        # RX state
        self.vr = 0                 # next expected seq
        self.reasm = bytearray()
        self.ack_owed = False
        self.ack_deadline = 0.0

        # link / rate
        self.t_rx = 0.0
        self.t_tx = 0.0
        self.t_ack = 0.0
        self.t_err = 0.0
        self.peer_snr = 0.0
        self.tx_mode = cfg.mode if cfg.mode in MODES else "qpsk"
        self._rate_hold_until = 0.0
        self._up_since = None

        # stats
        self.n_tx = self.n_rx = self.n_retx = self.n_crc = 0
        self._pend = {}             # small reorder buffer: bonded lanes
                                    # legitimately deliver seq k+1 a hair
                                    # before seq k; hold instead of discard
        self.n_drop = 0
        self.tx_bytes_acc = self.rx_bytes_acc = 0
        self._rate_t = self.clock()
        self.tx_kbps = self.rx_kbps = 0.0

    # ------------------------------------------------------------- host TX
    def host_packet_in(self, pkt: bytes):
        if not pkt:
            return
        with self._lock:
            if (len(self.out_q) >= OUTQ_MAX_PKTS
                    or self.out_bytes + len(pkt) > OUTQ_MAX_BYTES):
                self.n_drop += 1
                return
            self.out_q.append((self.clock(), pkt))
            self.out_bytes += len(pkt)

    # --------------------------------------------------------- frame pull
    def next_frame(self, lane: int, now: float):
        """Returns (ftype, seq, ack, snr_db, payload, mode, mpx, fec) or None."""
        with self._lock:
            mode, mpx, use_fec = self.tx_mode, self.cfg.mpx, self.cfg.fec
            snr = self.local_snr()

            # 1. go-back-N resend of everything outstanding, triggered by
            #    either timeout (backstop) or duplicate ACKs (fast retx)
            if self.tx_store and not self.resend:
                t0 = self.tx_sent.get(self.va, 0.0)
                fast = self._fast_armed and self._dup_acks >= 3
                if fast:
                    self._fast_armed = False
                    self.n_fast += 1
                elif now - t0 > self._rto(mode):
                    self.n_timeout += 1
                if fast or now - t0 > self._rto(mode):
                    self._dup_acks = 0
                    s = self.va
                    while s != self.vs:
                        self.resend.append(s)
                        self._retxed.add(s)
                        s = (s + 1) & 0xFF
                    self.n_retx += len(self.resend)
                    self._rate_pressure(now)

            # 2. retransmissions first
            if self.resend:
                seq = self.resend.popleft()
                if seq in self.tx_store:
                    return self._emit_data(seq, now, snr, mode, mpx, use_fec)

            # 3. new data if window open
            if _delta(self.vs, self.va) < self.cfg.window:
                payload = self._pack_payload(now, self.max_payload(mode, use_fec))
                if payload:
                    seq = self.vs
                    self.vs = (self.vs + 1) & 0xFF
                    self.tx_store[seq] = payload
                    return self._emit_data(seq, now, snr, mode, mpx, use_fec)

            # 4. owed ACK
            if self.ack_owed and now >= self.ack_deadline:
                self.ack_owed = False
                self.t_tx = now
                return (FT_ACK, 0, self.vr, snr, b"", mode, mpx, use_fec)

            # 5. idle beacon (a slave waits to hear the master first)
            if now - self.t_tx > self.cfg.beacon_period:
                if self.cfg.role != "slave" or self.link_up(now):
                    self.t_tx = now
                    return (FT_BEACON, 0, self.vr, snr, b"", mode, mpx, use_fec)
            return None

    def _emit_data(self, seq: int, now: float, snr, mode, mpx, use_fec):
        self.tx_sent[seq] = now
        self.ack_owed = False
        self.t_tx = now
        self.n_tx += 1
        p = self.tx_store[seq]
        self.tx_bytes_acc += len(p)
        return (FT_DATA, seq, self.vr, snr, p, mode, mpx, use_fec)

    def _pack_payload(self, now: float, cap: int) -> bytes:
        """Aggregate / fragment queued packets into one frame payload."""
        if not self._frag_rest and not self.out_q:
            return b""
        if not self._frag_rest:
            oldest = self.out_q[0][0]
            full = self.out_bytes + PDU_HDR * len(self.out_q) >= cap
            if not full and (now - oldest) * 1000.0 < self.cfg.aggregate_ms:
                return b""                       # wait for more to aggregate
        out = bytearray()
        while len(out) + PDU_HDR < cap:
            if self._frag_rest:
                data = self._frag_rest
            elif self.out_q:
                _, data = self.out_q.popleft()
                self.out_bytes -= len(data)
            else:
                break
            room = cap - len(out) - PDU_HDR
            chunk, rest = data[:room], data[room:]
            flags = MORE if rest else 0
            out += struct.pack(">HB", len(chunk), flags) + chunk
            self._frag_rest = rest
            if rest:
                break
        return bytes(out)

    # ----------------------------------------------------------- frame RX
    def on_frame(self, hdr, payload: bytes | None, lane: int, now: float):
        with self._lock:
            self.t_rx = now
            self.peer_snr = (0.7 * self.peer_snr + 0.3 * hdr.snr_db
                             if self.peer_snr else hdr.snr_db)
            self._process_ack(hdr.ack, now)
            self._auto_rate(now)

            if hdr.ftype == FT_PROBE:
                self.ack_owed = True
                self.ack_deadline = now
                return
            if hdr.length == 0:
                return                                   # ACK / beacon: done
            if payload is None:                          # payload CRC failed
                self.n_crc += 1
                self.t_err = now
                return
            self.n_rx += 1
            if hdr.seq == self.vr:
                self.vr = (self.vr + 1) & 0xFF
                self.rx_bytes_acc += len(payload)
                self._deliver(payload)
                # drain anything the reorder buffer was holding
                while self.vr in self._pend:
                    p = self._pend.pop(self.vr)
                    self.vr = (self.vr + 1) & 0xFF
                    self.rx_bytes_acc += len(p)
                    self._deliver(p)
                self.ack_owed = True
                self.ack_deadline = now + self.cfg.ack_delay
            elif 1 <= _delta(hdr.seq, self.vr) <= 6:
                # ahead of the in-order point: lane skew or a gap from a
                # lost frame -- hold it, ack on the normal delayed timer
                if len(self._pend) < 8:
                    self._pend[hdr.seq] = payload
                self.ack_owed = True
                self.ack_deadline = min(self.ack_deadline,
                                        now + self.cfg.ack_delay) \
                    if self.ack_owed else now + self.cfg.ack_delay
            else:
                # stale duplicate behind vr: re-ack immediately so the
                # peer stops resending
                self.ack_owed = True
                self.ack_deadline = now

    def _rto(self, mode: str) -> float:
        # the oldest unacked frame can't be acknowledged until everything
        # queued ahead of it has been serialized onto the wire, so the
        # timeout must scale with the number of outstanding frames -- not
        # doing this causes spurious go-back-N storms at large windows
        outstanding = max(_delta(self.vs, self.va), 1)
        base = ((outstanding + 2.0) * self.frame_dur(mode)
                + 2 * self.cfg.ack_delay)
        if self.srtt is not None:
            base = max(base, 1.7 * self.srtt)
        return min(max(base, self.cfg.retx_timeout), 6.0)

    def _process_ack(self, ack: int, now: float):
        d = _delta(ack, self.va)
        if d == 0 and self.vs != self.va:
            # peer is repeating its cumulative ACK while we have frames
            # outstanding: it may have received something newer out of
            # order (or this is just its bonded twin / idle chatter)
            if now - self._dup_t > 0.5 * self.frame_dur(self.tx_mode):
                self._dup_acks += 1
                self._dup_t = now
        if 1 <= d <= self.cfg.window:
            self._dup_acks = 0
            self._fast_armed = True
            newest = (ack - 1) & 0xFF
            if newest not in self._retxed and newest in self.tx_sent:
                rtt = now - self.tx_sent[newest]
                self.srtt = rtt if self.srtt is None else 0.8 * self.srtt + 0.2 * rtt
            while self.va != ack:
                self.tx_store.pop(self.va, None)
                self.tx_sent.pop(self.va, None)
                self._retxed.discard(self.va)
                self.va = (self.va + 1) & 0xFF
            self.resend = deque(s for s in self.resend if s in self.tx_store)
            self.t_ack = now
            if self.tx_store:
                self.tx_sent[self.va] = now              # restart timer

    def _deliver(self, payload: bytes):
        i, n = 0, len(payload)
        while i + PDU_HDR <= n:
            ln, fl = struct.unpack(">HB", payload[i:i + PDU_HDR])
            i += PDU_HDR
            if ln == 0 or i + ln > n:
                break
            self.reasm += payload[i:i + ln]
            i += ln
            if len(self.reasm) > REASM_MAX:
                self.reasm.clear()
                continue
            if not (fl & MORE):
                pkt = bytes(self.reasm)
                self.reasm.clear()
                try:
                    self.send_host(pkt)
                except Exception:
                    pass

    # ---------------------------------------------------------- auto rate
    def _rate_pressure(self, now: float):
        """Called on go-back-N timeout: bias downwards."""
        if self.cfg.mode != "auto":
            return
        i = MODES.index(self.tx_mode)
        if i > 0 and now - self.t_ack > 2 * self._rto(self.tx_mode):
            self.tx_mode = MODES[i - 1]
            self._rate_hold_until = now + 3.0
            self._up_since = None
            self.log(f"auto-rate: down to {self.tx_mode} (retx)")

    def _auto_rate(self, now: float):
        if self.cfg.mode != "auto":
            self.tx_mode = self.cfg.mode if self.cfg.mode in MODES else self.tx_mode
            return
        m = self.tx_mode
        if m in AUTO_DOWN and self.peer_snr < AUTO_DOWN[m]:
            i = MODES.index(m)
            self.tx_mode = MODES[i - 1]
            self._rate_hold_until = now + 3.0
            self._up_since = None
            self.log(f"auto-rate: down to {self.tx_mode} (snr {self.peer_snr:.1f})")
            return
        if m in AUTO_UP and self.peer_snr > AUTO_UP[m] + 1.0:
            if now < self._rate_hold_until:
                return
            if self._up_since is None:
                self._up_since = now
            elif now - self._up_since > 2.0:
                i = MODES.index(m)
                self.tx_mode = MODES[i + 1]
                self._up_since = None
                self.log(f"auto-rate: up to {self.tx_mode} (snr {self.peer_snr:.1f})")
        else:
            self._up_since = None

    # -------------------------------------------------------------- misc
    def link_up(self, now: float) -> bool:
        return (now - self.t_rx) < self.cfg.link_timeout

    def stats(self) -> dict:
        with self._lock:
            now = self.clock()
            dt = now - self._rate_t
            if dt >= 1.0:
                self.tx_kbps = 0.6 * self.tx_kbps + 0.4 * (self.tx_bytes_acc * 8 / dt / 1000)
                self.rx_kbps = 0.6 * self.rx_kbps + 0.4 * (self.rx_bytes_acc * 8 / dt / 1000)
                self.tx_bytes_acc = self.rx_bytes_acc = 0
                self._rate_t = now
            return dict(tx_kbps=self.tx_kbps, rx_kbps=self.rx_kbps,
                        link=self.link_up(now), mode=self.tx_mode,
                        peer_snr=self.peer_snr, va=self.va, vs=self.vs,
                        vr=self.vr, n_tx=self.n_tx, n_rx=self.n_rx,
                        n_retx=self.n_retx, n_crc=self.n_crc,
                        n_drop=self.n_drop, qlen=len(self.out_q),
                        srtt=self.srtt or 0.0,
                        t_tx=self.t_tx, t_rx=self.t_rx, t_ack=self.t_ack,
                        t_err=self.t_err)
