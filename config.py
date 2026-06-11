"""Central configuration for the audio modem.

Everything tunable lives here.  The OFDM (DMT) numerology mirrors ADSL:
a real-valued multicarrier signal built with a Hermitian-symmetric IFFT,
which is the natural fit for a soundcard (a real baseband channel,
~20 kHz wide, with unknown gain/phase response -> per-bin equalization).
"""
from dataclasses import dataclass, field

MAGIC = 0xA7
VERSION = 1

# Frame types
FT_DATA = 0
FT_ACK = 1
FT_BEACON = 2
FT_PROBE = 3

FT_NAMES = {FT_DATA: "DATA", FT_ACK: "ACK", FT_BEACON: "BEACON", FT_PROBE: "PROBE"}

# Constellations (square Gray QAM; 256+ are realistic on a direct cable
# at 96/192 kHz with 24-bit converters)
MODES = ("qpsk", "16qam", "64qam", "256qam", "1024qam", "4096qam")
MODE_BITS = {"qpsk": 2, "16qam": 4, "64qam": 6,
             "256qam": 8, "1024qam": 10, "4096qam": 12}
MODE_ID = {m: i for i, m in enumerate(MODES)}
ID_MODE = {v: k for k, v in MODE_ID.items()}

# Auto rate thresholds (dB of measured data-path SNR).
# UP: SNR needed to step up FROM this mode; DOWN: below this, step down.
AUTO_UP = {"qpsk": 18.0, "16qam": 25.0, "64qam": 31.0,
           "256qam": 37.5, "1024qam": 44.0}
AUTO_DOWN = {"16qam": 14.0, "64qam": 21.0, "256qam": 27.5,
             "1024qam": 34.0, "4096qam": 41.0}


@dataclass
class ModemConfig:
    # --- Audio / physical layer ------------------------------------------
    fs: int = 48000            # sample rate; 192000 = wideband speed profile
    nfft: int = 512            # DMT FFT size
    cp: int = 64               # cyclic prefix (samples); absorbs filters + drift
    f_lo: float = None         # lowest used Hz (None -> derived from fs)
    f_hi: float = None         # highest used Hz (None -> derived from fs)
    pilot_step: int = 8        # every Nth used bin is a pilot
    tx_rms: float = 0.13       # TX RMS level (full scale = 1.0); OFDM PAPR ~12 dB
    clip: float = 0.95         # soft limit for peaks
    guard_samples: int = 288   # silence between frames (6 ms)

    # --- Link layer --------------------------------------------------------
    mode: str = "qpsk"         # qpsk | 16qam | 64qam | auto
    fec: bool = True           # Reed-Solomon RS(255,223) + interleaving
    mpx: bool = True           # subband multiplex (split each channel lo/hi)
    bond: bool = True          # use both stereo channels (L+R lanes)
    role: str = "peer"         # master | slave | peer
    max_data_syms: int = 24    # cap per frame (latency vs. overhead)
    aggregate_ms: float = 25.0 # wait this long to batch host packets per frame
    window: int = None         # go-back-N window (None -> 8, or 16 at >=96k)
    retx_timeout: float = 0.3  # floor for the adaptive retransmit timeout
    ack_delay: float = 0.05    # delayed-ack timer (piggyback opportunity)
    beacon_period: float = 1.0 # idle beacon interval (keeps SYNC/LINK lit)
    link_timeout: float = 4.0  # no frames for this long -> LINK down

    # --- Host interface ------------------------------------------------------
    hostif: str = "pty"        # pty | tcp | tun | serial | none
    framing: str = "slip"      # slip | kiss | raw   (for pty/tcp/serial)
    tcp_port: int = 8001       # KISS/SLIP-over-TCP listen port
    serial_port: str = ""      # e.g. COM5 (Windows + com0com)
    tun_ip: str = "10.55.0.1/24"
    tun_peer: str = "10.55.0.2"
    mtu_hint: int = 576

    # --- Devices --------------------------------------------------------------
    in_device: object = None   # sounddevice index/name or None = default
    out_device: object = None
    blocksize: int = None      # None -> ~21 ms (1024 at 48 k, 4096 at 192 k)

    def __post_init__(self):
        wide = self.fs >= 96000
        if self.f_lo is None:
            # at 192 k the band is huge; skipping hum/1/f territory is free
            self.f_lo = 3000.0 if wide else 300.0
        if self.f_hi is None:
            # consumer codecs are flat to ~0.45-0.47 fs at 96/192 k;
            # at 48 k stay under typical 20 kHz analog rolloff
            self.f_hi = 0.455 * self.fs if wide else 18000.0
        if self.window is None:
            self.window = 16 if wide else 8
        if self.blocksize is None:
            self.blocksize = max(1024, int(round(1024 * self.fs / 48000)))
        if wide and self.mtu_hint == 576:
            self.mtu_hint = 1500
        if wide and self.max_data_syms == 24:
            self.max_data_syms = 32

    # --- Derived (filled by OFDM) -------------------------------------------
    def sym_len(self) -> int:
        return self.nfft + self.cp

    def sym_rate(self) -> float:
        return self.fs / self.sym_len()


def lane_count(cfg: ModemConfig) -> int:
    return 2 if cfg.bond else 1
