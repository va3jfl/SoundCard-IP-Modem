"""DSP core: a real-valued multicarrier (DMT) modem, ADSL-style.

TX symbol = IFFT of a Hermitian-symmetric spectrum -> real samples,
cyclic prefix prepended.  Frame on the wire (per audio channel):

   [ preamble A ][ preamble B ][ header sym ][ data syms ... ][ guard ]

preamble A : energy on even bins only -> two identical time halves.
             Schmidl & Cox autocorrelation gives coarse timing; a
             cross-correlation against the known waveform refines it.
preamble B : known PN-QPSK on every used bin -> least-squares channel
             estimate H[k] (absorbs soundcard gain, phase, cable).
header     : QPSK, 3x bit repetition + CRC16 -> tells RX the mode,
             FEC/MPX flags and number of data symbols that follow.
data       : QPSK / 16-QAM / 64-QAM on data bins; every 8th bin is a
             pilot used to track common phase error and timing drift
             (sound cards on two machines never share a clock; the
             linear phase-vs-bin slope measures the drift each symbol).

MPX mode permutes the data-bin fill order to alternate low/high
subband, so consecutive coded bytes land in different subbands
(frequency diversity), and lets the UI meter the subbands separately.
"""
from __future__ import annotations
import numpy as np
from .config import ModemConfig, MODE_BITS

# ------------------------------------------------------------- QAM maps
def _axis_levels(nbits):
    m = 1 << nbits
    return (2 * np.arange(m) - (m - 1)).astype(np.float64)


class StreamResampler:
    """Continuous-state fractional resampler: polyphase 4x FIR upsample
    followed by a Catmull-Rom fractional reader.

    `ppm` may be changed between calls; +ppm consumes input faster
    (output time-compressed, frequencies scaled up by 1+ppm*1e-6).
    The anti-image filter passband is sized from f_pass so wideband
    profiles (carriers near 0.46*fs) survive intact.
    """

    def __init__(self, fs: float, f_pass: float, atten_db: float = 75.0):
        # higher oversampling at wideband rates: the fractional stage's
        # error falls steeply with normalized frequency
        up = self.UP = 8 if fs >= 96000 else 4
        fp = f_pass / (up * fs)                 # normalized passband edge
        fst = (fs - f_pass) / (up * fs)         # first zero-stuff image
        fst = max(fst, fp + 0.002)
        width = fst - fp
        ntaps = int(np.ceil((atten_db - 8) / (2.285 * 2 * np.pi * width)))
        ntaps = min(max(ntaps | 1, 31), 1023)   # odd, bounded
        fc = (fp + fst) / 2.0
        n = np.arange(ntaps) - (ntaps - 1) / 2
        beta = 0.1102 * (atten_db - 8.7) if atten_db > 50 else 5.0
        h = np.sinc(2 * fc * n) * np.kaiser(ntaps, beta)
        self._h = (h / h.sum() * up).astype(np.float64)
        # FFT overlap-save state for the (long) anti-image filter
        self._nfft_os = 1 << int(np.ceil(np.log2(4 * ntaps)))
        self._H = np.fft.rfft(self._h, self._nfft_os)
        self._zi = np.zeros(ntaps - 1)
        self._hist = np.zeros(0)
        self._t = 2.0
        self.ppm = 0.0

    def _up4(self, x: np.ndarray) -> np.ndarray:
        up = self.UP
        z = np.zeros(up * len(x))
        z[::up] = x
        buf = np.concatenate([self._zi, z])
        nh = len(self._h)
        # overlap-save FFT convolution, 'valid' part only
        step = self._nfft_os - (nh - 1)
        outs = []
        pos = 0
        while pos + nh - 1 < len(buf):
            seg = buf[pos:pos + self._nfft_os]
            pad = self._nfft_os - len(seg)
            if pad:
                seg = np.concatenate([seg, np.zeros(pad)])
            y = np.fft.irfft(np.fft.rfft(seg) * self._H, self._nfft_os)
            valid = min(step, len(buf) - (nh - 1) - pos)
            outs.append(y[nh - 1:nh - 1 + valid])
            pos += valid
        self._zi = buf[len(buf) - (nh - 1):]
        return np.concatenate(outs) if outs else np.zeros(0)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64)
        if len(x) == 0:
            return x
        self._hist = np.concatenate([self._hist, self._up4(x)])
        step = self.UP * (1.0 + self.ppm * 1e-6)
        limit = len(self._hist) - 3.0           # need idx+3 in range
        if self._t >= limit:
            return np.zeros(0)
        m = int((limit - self._t) // step) + 1
        ts = self._t + step * np.arange(m)
        ts = ts[ts < limit]
        if len(ts) == 0:
            return np.zeros(0)
        i = ts.astype(np.int64)
        f = ts - i
        # 6-point windowed-sinc fractional interpolation (better HF
        # accuracy than cubic; matters for 4096-QAM near the band edge)
        y = np.zeros(len(ts))
        wsum = np.zeros(len(ts))
        for j in range(-2, 4):
            d = j - f
            w = np.sinc(d) * (np.cos(np.pi * d / 6.0) ** 2)
            y += w * self._hist[i + j]
            wsum += w
        y /= wsum
        t_next = ts[-1] + step
        # never claim to consume samples that haven't arrived yet: cap at
        # the buffer length and carry the overshoot forward in _t, keeping
        # read positions continuous across call boundaries
        consumed = min(max(int(t_next) - 2, 0), len(self._hist))
        self._hist = self._hist[consumed:]
        self._t = t_next - consumed
        return y


def _gray_seq(nbits):
    i = np.arange(1 << nbits)
    return i ^ (i >> 1)            # binary-reflected Gray, natural -> gray


class QAM:
    """Gray-coded square QAM (any even bits/sym up to 12), unit avg power."""

    def __init__(self, bits_per_sym: int):
        if bits_per_sym % 2 or not (2 <= bits_per_sym <= 12):
            raise ValueError(bits_per_sym)
        self.bps = bits_per_sym
        self.axis_bits = bits_per_sym // 2
        self.gray = _gray_seq(self.axis_bits)
        self.gray_inv = np.argsort(self.gray)
        lv = _axis_levels(self.axis_bits)
        self.norm = np.sqrt(np.mean(lv ** 2) * 2)
        self.levels = lv / self.norm

    def map(self, bits: np.ndarray) -> np.ndarray:
        """bits (len % bps == 0) -> complex symbols, unit avg power."""
        b = bits.reshape(-1, self.bps)
        half = self.axis_bits
        w = 1 << np.arange(half - 1, -1, -1)
        i_idx = b[:, :half] @ w
        q_idx = b[:, half:] @ w
        return self.levels[self.gray_inv[i_idx]] + 1j * self.levels[self.gray_inv[q_idx]]

    def demap(self, sym: np.ndarray) -> np.ndarray:
        """complex symbols -> hard bits."""
        m = 1 << self.axis_bits

        def axis(v):
            idx = np.clip(np.round((v * self.norm + (m - 1)) / 2), 0, m - 1).astype(np.int64)
            g = self.gray[idx]
            out = np.zeros((len(v), self.axis_bits), dtype=np.uint8)
            for i in range(self.axis_bits):
                out[:, self.axis_bits - 1 - i] = (g >> i) & 1
            return out

        bi = axis(sym.real)
        bq = axis(sym.imag)
        return np.concatenate([bi, bq], axis=1).flatten()


# --------------------------------------------------------------- OFDM core
class OFDM:
    def __init__(self, cfg: ModemConfig):
        self.cfg = cfg
        n, fs = cfg.nfft, cfg.fs
        k_lo = int(np.ceil(cfg.f_lo * n / fs))
        k_hi = int(np.floor(cfg.f_hi * n / fs))
        k_hi = min(k_hi, n // 2 - 1)
        self.used = np.arange(k_lo, k_hi + 1)
        self.n_used = len(self.used)
        self.pilot_pos = np.arange(0, self.n_used, cfg.pilot_step)
        mask = np.ones(self.n_used, dtype=bool)
        mask[self.pilot_pos] = False
        self.data_pos = np.nonzero(mask)[0]
        self.n_data = len(self.data_pos)
        self.sym = n + cfg.cp

        # MPX bin fill order: alternate low-half / high-half data bins
        half = self.n_data // 2
        lo, hi = self.data_pos[:half], self.data_pos[half:half * 2]
        inter = np.empty(half * 2, dtype=np.int64)
        inter[0::2], inter[1::2] = lo, hi
        if self.n_data % 2:
            inter = np.append(inter, self.data_pos[-1])
        self.data_pos_mpx = inter
        self.mpx_lo_mask = self.used[self.data_pos] < (k_lo + k_hi) // 2

        rng = np.random.default_rng(0x0FD1)
        qpsk = QAM(2)
        # preamble A: BPSK on even used bins (-> two identical halves)
        self.even_pos = np.nonzero(self.used % 2 == 0)[0]
        pa = np.zeros(self.n_used, dtype=complex)
        pa[self.even_pos] = (rng.integers(0, 2, len(self.even_pos)) * 2 - 1) * np.sqrt(2)
        # preamble B: PN QPSK on all used bins (channel estimation)
        pb = qpsk.map(rng.integers(0, 2, self.n_used * 2))
        self.pre_a_vals, self.pre_b_vals = pa, pb
        # pilot PN table, 16 symbol patterns
        self.pilot_tab = qpsk.map(rng.integers(0, 2, (16, len(self.pilot_pos) * 2)).flatten()
                                  ).reshape(16, len(self.pilot_pos))
        self.qams = {b: QAM(b) for b in sorted(set(MODE_BITS.values()))}

        # fixed TX gain so the constellation scale never moves
        probe = self._ifft_sym(pb)
        self.gain = cfg.tx_rms / max(probe.std(), 1e-9)
        self.pre_a_wave = self._mod(self.pre_a_vals)          # with CP
        self.pre_b_wave = self._mod(self.pre_b_vals)
        self.pre_a_core = self.pre_a_wave[cfg.cp:]            # N samples, template

    # ---- symbol-level
    def _ifft_sym(self, vals: np.ndarray) -> np.ndarray:
        n = self.cfg.nfft
        spec = np.zeros(n, dtype=complex)
        spec[self.used] = vals
        spec[n - self.used] = np.conj(vals)
        return np.fft.ifft(spec).real * n / np.sqrt(self.n_used * 2)

    def _mod(self, vals: np.ndarray) -> np.ndarray:
        x = self._ifft_sym(vals) * self.gain
        return np.concatenate([x[-self.cfg.cp:], x])

    def fft_bins(self, samples: np.ndarray) -> np.ndarray:
        return np.fft.fft(samples)[self.used]

    def sym_vals(self, sym_idx: int, data_syms: np.ndarray, mpx: bool) -> np.ndarray:
        vals = np.zeros(self.n_used, dtype=complex)
        vals[self.pilot_pos] = self.pilot_tab[sym_idx % 16]
        pos = self.data_pos_mpx if mpx else self.data_pos
        vals[pos] = data_syms
        return vals

    # ---- TX frame
    def bits_per_data_sym(self, mode: str) -> int:
        return self.n_data * MODE_BITS[mode]

    def mod_frame(self, header_bits: np.ndarray, data_bits: np.ndarray | None,
                  mode: str, mpx: bool) -> np.ndarray:
        """header_bits: exactly n_data*2 (QPSK). data_bits padded to whole syms."""
        qpsk = self.qams[2]
        parts = [self.pre_a_wave, self.pre_b_wave]
        hvals = self.sym_vals(0, qpsk.map(header_bits), False)
        parts.append(self._mod(hvals))
        if data_bits is not None and len(data_bits):
            qam = self.qams[MODE_BITS[mode]]
            bps = self.bits_per_data_sym(mode)
            nsym = len(data_bits) // bps
            for i in range(nsym):
                chunk = data_bits[i * bps:(i + 1) * bps]
                parts.append(self._mod(self.sym_vals(i + 1, qam.map(chunk), mpx)))
        parts.append(np.zeros(self.cfg.guard_samples))
        out = np.concatenate(parts)
        np.clip(out, -self.cfg.clip, self.cfg.clip, out=out)
        return out.astype(np.float32)

    def frame_samples(self, n_data_syms: int) -> int:
        return (3 + n_data_syms) * self.sym


# --------------------------------------------------------- RX state machine
HUNT, HEADER, DATA = 0, 1, 2


class Demod:
    """Per-lane receiver.  Feed audio blocks; emits decoded frames via
    callbacks supplied by the framing layer:

      header_cb(hard_bits) -> (n_data_syms, mode, mpx)  or None if invalid
      frame_cb(data_bits | None, metrics)
    """

    def __init__(self, ofdm: OFDM, header_cb, frame_cb, name="L"):
        self.o = ofdm
        self.cfg = ofdm.cfg
        self.header_cb = header_cb
        self.frame_cb = frame_cb
        self.name = name
        self.buf = np.zeros(0, dtype=np.float64)
        self.state = HUNT
        self.metrics = dict(snr=0.0, level=-90.0, drift_ppm=0.0,
                            snr_lo=0.0, snr_hi=0.0)
        self.const_points: list[complex] = []
        self._reset_frame()
        L = self.cfg.nfft // 2
        self._min_lvl = 1e-3
        self._sc_thresh = 0.55
        self._L = L
        self.timing_adv = 10
        # closed-loop sample-rate-offset correction: a fractional resampler
        # ahead of the demod, steered by the residual drift measured from
        # pilot timing.  Essential for 256-QAM+ where carriers sit at high
        # bin indices (SFO-induced ICI scales with ppm * bin).
        self.rsmp = StreamResampler(self.cfg.fs, self.cfg.f_hi * 1.03)
        self._sfo_resid = 0.0

    def _reset_frame(self):
        self.H = None
        self.noise = 1e-9
        self.fstart = 0
        self.nsyms = 0
        self.mode = "qpsk"
        self.mpx = False
        self.sym_i = 0
        self.bits = []

    # ---------- public
    def feed(self, samples: np.ndarray):
        lvl = np.sqrt(np.mean(samples ** 2) + 1e-20) if len(samples) else 0.0
        if len(samples):
            self.metrics["level"] = 20 * np.log10(lvl + 1e-10)
        samples = self.rsmp(samples)
        if len(samples) == 0:
            return
        self.buf = np.concatenate([self.buf, samples.astype(np.float64)])
        progress = True
        while progress:
            progress = False
            if self.state == HUNT:
                progress = self._hunt()
            elif self.state == HEADER:
                progress = self._header()
            elif self.state == DATA:
                progress = self._data()
        # keep buffer bounded
        keep = 4 * self.cfg.fs
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            self.buf = self.buf[cut:]
            if self.state != HUNT:
                self.fstart -= cut
                if self.fstart < 0:
                    self.state = HUNT
                    self._reset_frame()

    # ---------- stages
    def _hunt(self) -> bool:
        L, sym = self._L, self.o.sym
        need = 2 * L + sym + self.cfg.cp
        if len(self.buf) < need + 256:
            return False
        x = self.buf
        # Schmidl-Cox metric over the searchable range
        nmax = len(x) - 2 * L
        y = x[:nmax + L] * x[L:nmax + 2 * L]
        e = x[L:nmax + 2 * L] ** 2
        cy = np.concatenate([[0.0], np.cumsum(y)])
        ce = np.concatenate([[0.0], np.cumsum(e)])
        P = cy[L:] - cy[:-L]            # len nmax+1
        R = ce[L:] - ce[:-L]
        M = (P * P) / (R * R + 1e-12)
        gate = R > (self._min_lvl ** 2) * L
        cand = np.nonzero((M > self._sc_thresh) & gate)[0]
        if len(cand) == 0:
            # nothing yet; drop all but a tail we may still need
            tail = 2 * L + 256
            if len(self.buf) > tail:
                self.buf = self.buf[len(self.buf) - tail:]
            return False
        d0 = int(cand[0])
        # find the plateau peak in the next CP+L region
        hi = min(d0 + self.cfg.cp + L, len(M))
        dpk = d0 + int(np.argmax(M[d0:hi]))
        # fine timing: cross-correlate with the known preamble-A core
        srch_lo = max(dpk - self.cfg.cp, 0)
        srch_hi = dpk + self.cfg.cp
        n = self.cfg.nfft
        if srch_hi + n > len(x):
            return False  # wait for more samples
        tpl = self.o.pre_a_core
        best, bidx = -1.0, srch_lo
        seg_all = x[srch_lo:srch_hi + n]
        # normalized cross-correlation
        c = np.correlate(seg_all, tpl, mode="valid")
        e2 = np.convolve(seg_all ** 2, np.ones(n), mode="valid")
        nc = np.abs(c) / np.sqrt(e2 * np.sum(tpl ** 2) + 1e-12)
        bidx = srch_lo + int(np.argmax(nc))
        best = float(np.max(nc))
        if best < 0.35:
            # false alarm; skip past it
            self.buf = self.buf[dpk + L:]
            return True
        self.fstart = bidx - self.cfg.cp   # start of preamble A (with CP)
        if self.fstart < 0:
            self.fstart = 0
        # noise estimate from the two identical halves of preamble A
        a = x[bidx:bidx + L]
        b = x[bidx + L:bidx + 2 * L]
        nvar = float(np.mean((a - b) ** 2) / 2)
        svar = max(float(np.mean(a ** 2)) - nvar, 1e-12)
        self.noise = max(nvar, 1e-12)
        self.metrics["snr"] = 10 * np.log10(svar / self.noise)
        self.state = HEADER
        return True

    def _take_sym(self, idx: int) -> np.ndarray | None:
        # window advanced into the CP: tolerates late drift (+ppm) up to
        # `timing_adv` samples and early drift up to cp - timing_adv.
        # The phase ramp this causes is absorbed by the channel estimate.
        s0 = self.fstart + idx * self.o.sym + self.cfg.cp - self.timing_adv
        s1 = s0 + self.cfg.nfft
        if s1 > len(self.buf):
            return None
        return self.o.fft_bins(self.buf[s0:s1])

    def _header(self) -> bool:
        Yb = self._take_sym(1)
        Yh = self._take_sym(2)
        if Yb is None or Yh is None:
            return False
        # channel estimate from preamble B (raw least-squares per bin)
        self.H = Yb / self.o.pre_b_vals
        self._last_tau = 0.0
        self._tau_syms = 0
        if np.mean(np.abs(self.H)) < 1e-6:
            self._abort()
            return True
        Xh = Yh / self.H
        Xh = self._pilot_correct(Xh, 0)
        bits = self.o.qams[2].demap(Xh[self.o.data_pos])
        res = self.header_cb(bits)
        if res is None:
            self._abort()
            return True
        self.nsyms, self.mode, self.mpx = res
        if self.nsyms == 0:
            self.frame_cb(None, dict(self.metrics))
            self._advance(0)
            return True
        self.sym_i = 0
        self.bits = []
        self.state = DATA
        return True

    def _data(self) -> bool:
        moved = False
        qam = self.o.qams[MODE_BITS[self.mode]]
        pos = self.o.data_pos_mpx if self.mpx else self.o.data_pos
        while self.sym_i < self.nsyms:
            Y = self._take_sym(3 + self.sym_i)
            if Y is None:
                return moved
            X = Y / self.H
            X = self._pilot_correct(X, self.sym_i + 1)
            d = X[pos]
            self.bits.append(qam.demap(d))
            if self.sym_i % 2 == 0 and len(self.const_points) < 800:
                self.const_points.extend(d[::4].tolist())
            self._subband_snr(X)
            self.sym_i += 1
            moved = True
        data_bits = np.concatenate(self.bits) if self.bits else None
        self._sfo_update()
        self.frame_cb(data_bits, dict(self.metrics))
        self._advance(self.nsyms)
        return True

    # ---------- helpers
    def _sfo_update(self):
        resid = self.metrics["drift_ppm"]
        # integrate toward zero residual, only when this frame actually
        # produced a fresh timing measurement (data frames, not beacons)
        if getattr(self, "_drift_fresh", False) and abs(resid) > 0.7:
            step = float(np.clip(-0.6 * resid, -150.0, 150.0))
            self.rsmp.ppm = float(np.clip(self.rsmp.ppm + step, -600.0, 600.0))
        self._drift_fresh = False
        self.metrics["sfo_corr_ppm"] = self.rsmp.ppm
        self.metrics["drift_line_ppm"] = -self.rsmp.ppm + resid

    def _pilot_correct(self, X: np.ndarray, sym_idx: int) -> np.ndarray:
        pp = self.o.pilot_pos
        ref = self.o.pilot_tab[sym_idx % 16]
        e = X[pp] * np.conj(ref)            # ~ e^{j(a + b k)} + noise
        k = self.o.used[pp].astype(np.float64)
        # slope from adjacent pilot pairs
        dphi = np.angle(np.sum(e[1:] * np.conj(e[:-1])))
        dk = float(np.mean(np.diff(k)))
        b = dphi / dk
        a = np.angle(np.sum(e * np.exp(-1j * b * k)))
        corr = np.exp(-1j * (a + b * self.o.used.astype(np.float64)))
        Xc = X * corr
        # metrics
        ec = e * np.exp(-1j * (a + b * k))
        ev = float(np.mean(np.abs(ec - np.mean(ec)) ** 2)) + 1e-12
        sp = float(np.mean(np.abs(ec) ** 2))
        snr = 10 * np.log10(max(sp / ev, 1.0))
        self.metrics["snr"] = 0.8 * self.metrics["snr"] + 0.2 * min(snr, 48.0)
        tau = -b * self.cfg.nfft / (2 * np.pi)
        dsym = sym_idx - self._tau_syms
        if dsym > 0 and self._tau_syms > 0:
            ppm = -(tau - self._last_tau) / (dsym * self.o.sym) * 1e6
            self._drift_fresh = True
            self.metrics["drift_ppm"] = (0.7 * self.metrics["drift_ppm"]
                                         + 0.3 * float(np.clip(ppm, -500, 500)))
        self._last_tau = tau
        self._tau_syms = sym_idx
        return Xc

    def _subband_snr(self, X: np.ndarray):
        # decision-error EVM split by subband, for the MPX meters
        d = X[self.o.data_pos]
        qam = self.o.qams[MODE_BITS[self.mode]]
        hard = qam.map(qam.demap(d))
        err = np.abs(d - hard) ** 2 + 1e-12
        lo = self.o.mpx_lo_mask
        s_lo = 10 * np.log10(np.mean(np.abs(hard[lo]) ** 2) / np.mean(err[lo]))
        s_hi = 10 * np.log10(np.mean(np.abs(hard[~lo]) ** 2) / np.mean(err[~lo]))
        self.metrics["snr_lo"] = 0.8 * self.metrics["snr_lo"] + 0.2 * min(s_lo, 45)
        self.metrics["snr_hi"] = 0.8 * self.metrics["snr_hi"] + 0.2 * min(s_hi, 45)

    def _advance(self, nsyms: int):
        end = self.fstart + (3 + nsyms) * self.o.sym
        self.buf = self.buf[max(end, 0):]
        self.state = HUNT
        self._reset_frame()

    def _abort(self):
        # bad header: resume hunting just past the supposed preamble
        self.buf = self.buf[self.fstart + self._L:]
        self.state = HUNT
        self._reset_frame()
