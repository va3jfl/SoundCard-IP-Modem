"""Software loopback selftest: two modems wired through a simulated cable.

Runs without a soundcard.  Each direction gets an independent channel
(gain, FIR bandpass, AWGN, clock skew via ideal bandlimited resampling).
Random packets are injected at both ends; the test asserts that ARQ
delivers 100% of them intact and in order, and reports throughput and
pre-ARQ frame error rates.

    python3 -m audiomodem.selftest
"""
from __future__ import annotations

import time
import numpy as np

from .config import ModemConfig
from .modem import Modem
from .dsp import StreamResampler

FS = 48000.0


def _fir_lowpass(fs, f_hi=None):
    """Codec anti-alias rolloff: flat across the passband, transition
    above the modem band.  Kept SHORT like real converter filters so its
    impulse response fits comfortably inside the cyclic prefix."""
    f_hi = f_hi if f_hi else 0.487 * fs
    nt = 201 if fs >= 96000 else 121
    n = np.arange(nt) - (nt - 1) / 2
    h = 2 * f_hi / fs * np.sinc(2 * f_hi / fs * n) * np.kaiser(nt, 6.0)
    return h / h.sum()


class StreamChannel:
    """Streaming channel sim: clock skew -> FIR bandpass -> gain -> AWGN.

    Skew uses the same continuous-state resampler the receiver uses for
    SFO correction (dsp.StreamResampler), so both sides of that code see
    heavy test coverage.
    """

    def __init__(self, fs, snr_db=None, ppm=0.0, gain=0.5, seed=1,
                 f_lo=250.0, f_hi=None):
        self.fs = fs
        self.snr_db = snr_db
        self.gain = gain
        self.rng = np.random.default_rng(seed)
        f_hi = f_hi if f_hi else 0.475 * fs
        self.h = _fir_lowpass(fs, f_hi)
        self._zi = np.zeros(len(self.h) - 1)
        # AC coupling: 1-pole highpass like a real line input (~tens of Hz)
        self._hp_a = float(np.exp(-2 * np.pi * 120.0 / fs))
        self._hp_x1 = 0.0
        self._hp_y1 = 0.0
        self.ppm = ppm
        self.rs = StreamResampler(fs, f_hi * 1.02) if ppm else None
        if self.rs:
            self.rs.ppm = ppm

    def _fir(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return x
        buf = np.concatenate([self._zi, x])
        y = np.convolve(buf, self.h, mode="valid")
        self._zi = buf[len(buf) - (len(self.h) - 1):]
        return y

    def _hp(self, x: np.ndarray) -> np.ndarray:
        # y[n] = a*(y[n-1] + x[n] - x[n-1]), vectorized:
        # y[n] = a^(n+1)*y0 + sum_k a^(n+1-k) * (x[k]-x[k-1])
        if len(x) == 0:
            return x
        a = self._hp_a
        u = a * np.diff(np.concatenate([[self._hp_x1], x]))
        n = np.arange(1, len(x) + 1, dtype=np.float64)
        an = a ** n
        y = an * (self._hp_y1 + np.cumsum(u / an))
        self._hp_x1 = float(x[-1])
        self._hp_y1 = float(y[-1])
        return y

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64)
        if self.rs is not None:
            x = self.rs(x)
        y = self._fir(self._hp(x)) * self.gain
        if self.snr_db is not None and len(y):
            sig_p = (0.13 * self.gain) ** 2          # nominal in-frame power
            n_p = sig_p / 10 ** (self.snr_db / 10)
            y = y + self.rng.normal(0.0, np.sqrt(n_p), len(y))
        return y.astype(np.float32)


class Pipe:
    """Stereo pipe with independent per-channel sims and a sample queue."""

    def __init__(self, fs, snr_db, ppm, gain=0.5, seed=1,
                 f_lo=250.0, f_hi=None):
        self.chL = StreamChannel(fs, snr_db, ppm, gain, seed,
                                 f_lo=f_lo, f_hi=f_hi)
        self.chR = StreamChannel(fs, snr_db, ppm, gain, seed + 100,
                                 f_lo=f_lo, f_hi=f_hi)
        self.qL = np.zeros(0, np.float32)
        self.qR = np.zeros(0, np.float32)

    def push(self, stereo: np.ndarray):
        self.qL = np.concatenate([self.qL, self.chL(stereo[:, 0])])
        self.qR = np.concatenate([self.qR, self.chR(stereo[:, 1])])

    def pop(self, n: int) -> np.ndarray:
        """Return up to n stereo samples; never zero-fills (a real ADC
        delivers a contiguous stream, so neither does the sim)."""
        k = min(n, len(self.qL), len(self.qR))
        out = np.stack([self.qL[:k], self.qR[:k]], axis=1)
        self.qL = self.qL[k:]
        self.qR = self.qR[k:]
        return out


class Collector:
    def __init__(self):
        self.pkts = []

    def __call__(self, pkt: bytes):
        self.pkts.append(pkt)


class SimClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def run_case(name, mode, snr, ppm, *, fs=48000, mpx=True, bond=True,
             fec=True, npkts=30, sim_seconds=40.0, pkt_len=(40, 500), seed=5,
             inject_every=12, inject_burst=1, verbose=False):
    rng = np.random.default_rng(seed)
    clk = SimClock()
    cfgA = ModemConfig(fs=fs, mode=mode, mpx=mpx, bond=bond, fec=fec,
                       hostif="none")
    cfgB = ModemConfig(fs=fs, mode=mode, mpx=mpx, bond=bond, fec=fec,
                       hostif="none")
    A, B = Modem(cfgA, clock=clk), Modem(cfgB, clock=clk)
    colA, colB = Collector(), Collector()
    A.attach_host(colA)
    B.attach_host(colB)
    # codec model: AC-coupling HP well below the first carrier, LP rolloff
    # between the top carrier and Nyquist (flat across the used band)
    band = dict(f_lo=min(cfgA.f_lo * 0.5, cfgA.f_lo - 800),
                f_hi=(cfgA.f_hi + 0.493 * fs) / 2)
    a2b = Pipe(fs, snr, ppm, seed=seed, **band)
    b2a = Pipe(fs, snr, -ppm, seed=seed + 7, **band)

    sentA = [rng.integers(0, 256, rng.integers(*pkt_len), dtype=np.uint8
                          ).tobytes() for _ in range(npkts)]
    sentB = [rng.integers(0, 256, rng.integers(*pkt_len), dtype=np.uint8
                          ).tobytes() for _ in range(npkts)]

    blk = cfgA.blocksize
    steps = int(sim_seconds * fs / blk)
    inj = 0
    for step in range(steps):
        clk.t = step * blk / fs
        if step % inject_every == 0:
            for _ in range(inject_burst):
                if (inj < npkts and A.link.stats()["qlen"] < 24
                        and B.link.stats()["qlen"] < 24):
                    A.host_packet_in(sentA[inj])
                    B.host_packet_in(sentB[inj])
                    inj += 1
        a2b.push(A.pull_tx(blk))
        b2a.push(B.pull_tx(blk))
        B.push_rx(a2b.pop(blk))
        A.push_rx(b2a.pop(blk))
        if (len(colA.pkts) == npkts and len(colB.pkts) == npkts):
            break

    okA = colB.pkts == sentA[:len(colB.pkts)] and len(colB.pkts) == npkts
    okB = colA.pkts == sentB[:len(colA.pkts)] and len(colA.pkts) == npkts
    simt = (step + 1) * blk / fs
    payA = sum(map(len, colB.pkts)) * 8 / simt / 1000
    payB = sum(map(len, colA.pkts)) * 8 / simt / 1000
    sA, sB = A.link.stats(), B.link.stats()
    status = "PASS" if (okA and okB) else "FAIL"
    print(f"[{status}] {name:30s} A->B {len(colB.pkts):3d}/{npkts} "
          f"B->A {len(colA.pkts):3d}/{npkts}  {simt:5.1f}s sim  "
          f"goodput {payA:5.1f}/{payB:5.1f} kbit/s  "
          f"rx {sB['n_rx']}/{sA['n_rx']}  retx {sA['n_retx']}/{sB['n_retx']}  "
          f"crc {sB['n_crc']}/{sA['n_crc']}  mode {sA['mode']}/{sB['mode']}")
    if verbose:
        print("   A:", sA)
        print("   B:", sB)
    return okA and okB


def main():
    t0 = time.time()
    results = []
    results.append(run_case("64qam 30dB ±50ppm bond+mpx", "64qam", 30, 50))
    results.append(run_case("64qam 32dB clean", "64qam", 32, 0))
    results.append(run_case("16qam 22dB +-300ppm", "16qam", 22, 300))
    results.append(run_case("qpsk 12dB +-300ppm", "qpsk", 12, 300))
    results.append(run_case("16qam 20dB no-mpx no-bond", "16qam", 20, 50,
                            mpx=False, bond=False, npkts=15))
    results.append(run_case("auto-rate ramps up @28dB", "auto", 28, 20,
                            npkts=20))
    results.append(run_case("auto holds qpsk @18dB", "auto", 18, 0,
                            npkts=15))
    results.append(run_case("bulk throughput 64qam 32dB", "64qam", 32, 30,
                            npkts=120, pkt_len=(300, 501), inject_every=1,
                            sim_seconds=30.0))
    results.append(run_case("bulk throughput qpsk 15dB", "qpsk", 15, 100,
                            npkts=60, pkt_len=(300, 501), inject_every=1,
                            sim_seconds=30.0))
    # ---- 192 kHz wideband profile (24-bit cards, direct cable) ----
    results.append(run_case("192k 256qam 35dB +-60ppm", "256qam", 35, 60,
                            fs=192000, npkts=30, sim_seconds=20.0))
    results.append(run_case("192k 1024qam 43dB +-40ppm", "1024qam", 43, 40,
                            fs=192000, npkts=30, sim_seconds=20.0))
    results.append(run_case("192k auto ramps @40dB", "auto", 40, 25,
                            fs=192000, npkts=150, pkt_len=(400, 1200),
                            sim_seconds=28.0, inject_every=4))
    results.append(run_case("192k RECORD 1024qam 45dB", "1024qam", 45, 30,
                            fs=192000, npkts=400, pkt_len=(1200, 1401),
                            inject_every=1, inject_burst=3, sim_seconds=16.0))
    results.append(run_case("192k 4096qam 50dB bulk", "4096qam", 50, 10,
                            fs=192000, npkts=400, pkt_len=(1200, 1401),
                            inject_every=1, inject_burst=3, sim_seconds=16.0))
    dt = time.time() - t0
    print(f"\n{sum(results)}/{len(results)} cases passed in {dt:.0f}s wall")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
