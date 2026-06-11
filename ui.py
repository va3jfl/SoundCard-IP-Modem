"""Tkinter front panel, styled after rack modem hardware.

LEDs:  PWR SYNC-L SYNC-R LINK TX RX ACK ERR
Meters: per-lane SNR, input level, drift, throughput, constellation,
        and the four MPX subband SNR meters (L-lo L-hi R-lo R-hi).
Controls: mode (auto/qpsk/16qam/64qam), FEC, MPX, role, audio devices.
Bonding is fixed at launch (it defines the lane structure).
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from .config import MODES

BG = "#14171c"
PANEL = "#1d2128"
EDGE = "#2c323c"
FG = "#c9d1d9"
DIM = "#5a6470"
AMBER = "#ffb347"
GREEN = "#3ddc84"
RED = "#ff5252"
CYAN = "#4dd0e1"
FONT = ("DejaVu Sans Mono", 9)
FONTB = ("DejaVu Sans Mono", 10, "bold")


class Led:
    def __init__(self, parent, label, color=GREEN):
        self.frame = tk.Frame(parent, bg=PANEL)
        self.cv = tk.Canvas(self.frame, width=18, height=18, bg=PANEL,
                            highlightthickness=0)
        self.color = color
        self.oid = self.cv.create_oval(3, 3, 15, 15, fill="#3a3f47",
                                       outline="#0b0d10")
        self.cv.pack()
        tk.Label(self.frame, text=label, bg=PANEL, fg=DIM,
                 font=("DejaVu Sans Mono", 7)).pack()
        self._on = False

    def set(self, on: bool):
        if on != self._on:
            self._on = on
            self.cv.itemconfig(self.oid, fill=self.color if on else "#3a3f47")


class Bar:
    def __init__(self, parent, label, lo, hi, unit, color=GREEN, width=150):
        self.lo, self.hi, self.unit, self.color = lo, hi, unit, color
        self.frame = tk.Frame(parent, bg=PANEL)
        tk.Label(self.frame, text=f"{label:<6}", bg=PANEL, fg=DIM,
                 font=FONT).pack(side="left")
        self.cv = tk.Canvas(self.frame, width=width, height=12, bg="#0b0d10",
                            highlightthickness=1, highlightbackground=EDGE)
        self.cv.pack(side="left", padx=4)
        self.rid = self.cv.create_rectangle(0, 0, 0, 12, fill=color, width=0)
        self.txt = tk.Label(self.frame, text="", bg=PANEL, fg=FG, font=FONT,
                            width=11, anchor="w")
        self.txt.pack(side="left")
        self.width = width

    def set(self, value: float):
        f = max(0.0, min(1.0, (value - self.lo) / (self.hi - self.lo)))
        self.cv.coords(self.rid, 0, 0, int(f * self.width), 12)
        self.txt.config(text=f"{value:6.1f} {self.unit}")


class MpxMeter:
    """Tiny lo/hi subband LED+bar pair for one lane."""

    def __init__(self, parent, label):
        self.frame = tk.Frame(parent, bg=PANEL)
        tk.Label(self.frame, text=label, bg=PANEL, fg=DIM,
                 font=FONT).pack(side="left", padx=(0, 3))
        self.cv = tk.Canvas(self.frame, width=86, height=14, bg="#0b0d10",
                            highlightthickness=1, highlightbackground=EDGE)
        self.cv.pack(side="left")
        self.b_lo = self.cv.create_rectangle(2, 2, 2, 12, fill=CYAN, width=0)
        self.b_hi = self.cv.create_rectangle(44, 2, 44, 12, fill=AMBER, width=0)
        self.cv.create_line(43, 0, 43, 14, fill=EDGE)

    def set(self, snr_lo: float, snr_hi: float):
        f = lambda s: max(0.0, min(1.0, s / 35.0)) * 40
        self.cv.coords(self.b_lo, 2, 2, 2 + f(snr_lo), 12)
        self.cv.coords(self.b_hi, 44, 2, 44 + f(snr_hi), 12)


class ModemUI:
    def __init__(self, cfg, modem, audio=None, hostif=None, on_quit=None):
        self.cfg = cfg
        self.modem = modem
        self.audio = audio
        self.hostif = hostif
        self.on_quit = on_quit
        self._loglines = []

        self.root = tk.Tk()
        self.root.title("audiomodem  DMT-48  soundcard IP modem")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        outer = tk.Frame(self.root, bg=PANEL, highlightthickness=2,
                         highlightbackground=EDGE)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(outer, text="A U D I O M O D E M    DMT-48    "
                 "OFDM / stereo-bonded / RS-FEC",
                 bg=PANEL, fg=AMBER, font=FONTB).pack(anchor="w",
                                                      padx=10, pady=(8, 2))

        # ---- LED row
        ledrow = tk.Frame(outer, bg=PANEL)
        ledrow.pack(anchor="w", padx=10, pady=4)
        mk = lambda name, col: Led(ledrow, name, col)
        self.led = {
            "PWR": mk("PWR", GREEN), "SYNC L": mk("SYNC L", GREEN),
            "SYNC R": mk("SYNC R", GREEN), "LINK": mk("LINK", CYAN),
            "TX": mk("TX", AMBER), "RX": mk("RX", AMBER),
            "ACK": mk("ACK", GREEN), "ERR": mk("ERR", RED),
        }
        for w in self.led.values():
            w.frame.pack(side="left", padx=6)
        self.led["PWR"].set(True)

        # ---- middle: constellation + meters
        mid = tk.Frame(outer, bg=PANEL)
        mid.pack(fill="x", padx=10, pady=4)
        cfrm = tk.Frame(mid, bg=PANEL)
        cfrm.pack(side="left", padx=(0, 12))
        tk.Label(cfrm, text="CONSTELLATION (L)", bg=PANEL, fg=DIM,
                 font=("DejaVu Sans Mono", 7)).pack(anchor="w")
        self.const = tk.Canvas(cfrm, width=170, height=170, bg="#0b0d10",
                               highlightthickness=1, highlightbackground=EDGE)
        self.const.pack()
        for p in (0.25, 0.5, 0.75):
            self.const.create_line(170 * p, 0, 170 * p, 170, fill="#181d24")
            self.const.create_line(0, 170 * p, 170, 170 * p, fill="#181d24")

        meters = tk.Frame(mid, bg=PANEL)
        meters.pack(side="left", fill="x", expand=True)
        self.bar_snr_l = Bar(meters, "SNR L", 0, 40, "dB")
        self.bar_snr_l.frame.pack(anchor="w", pady=1)
        self.bar_snr_r = Bar(meters, "SNR R", 0, 40, "dB")
        self.bar_snr_r.frame.pack(anchor="w", pady=1)
        self.bar_lvl = Bar(meters, "LEVEL", -60, 0, "dBFS", color=CYAN)
        self.bar_lvl.frame.pack(anchor="w", pady=1)
        self.lbl_stats = tk.Label(meters, text="", bg=PANEL, fg=FG,
                                  font=FONT, justify="left", anchor="w")
        self.lbl_stats.pack(anchor="w", pady=(4, 0))

        mpxrow = tk.Frame(meters, bg=PANEL)
        mpxrow.pack(anchor="w", pady=(6, 0))
        tk.Label(mpxrow, text="MPX", bg=PANEL, fg=DIM,
                 font=FONT).pack(side="left", padx=(0, 6))
        self.mpx_l = MpxMeter(mpxrow, "L lo|hi")
        self.mpx_l.frame.pack(side="left", padx=4)
        self.mpx_r = MpxMeter(mpxrow, "R lo|hi")
        self.mpx_r.frame.pack(side="left", padx=4)

        # ---- controls
        ctl = tk.Frame(outer, bg=PANEL)
        ctl.pack(anchor="w", padx=10, pady=6)
        tk.Label(ctl, text="MODE", bg=PANEL, fg=DIM, font=FONT).pack(side="left")
        self.v_mode = tk.StringVar(value=cfg.mode)
        cb = ttk.Combobox(ctl, textvariable=self.v_mode, width=7,
                          values=("auto",) + MODES, state="readonly")
        cb.pack(side="left", padx=(2, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_mode())

        self.v_fec = tk.BooleanVar(value=cfg.fec)
        tk.Checkbutton(ctl, text="FEC", variable=self.v_fec, bg=PANEL, fg=FG,
                       selectcolor=BG, activebackground=PANEL, font=FONT,
                       command=self._set_flags).pack(side="left", padx=4)
        self.v_mpx = tk.BooleanVar(value=cfg.mpx)
        tk.Checkbutton(ctl, text="MPX", variable=self.v_mpx, bg=PANEL, fg=FG,
                       selectcolor=BG, activebackground=PANEL, font=FONT,
                       command=self._set_flags).pack(side="left", padx=4)
        bond = tk.Checkbutton(ctl, text=f"BOND ({'on' if cfg.bond else 'off'},"
                              " set at launch)", bg=PANEL, fg=DIM,
                              selectcolor=BG, font=FONT, state="disabled")
        bond.pack(side="left", padx=4)

        tk.Label(ctl, text="ROLE", bg=PANEL, fg=DIM, font=FONT
                 ).pack(side="left", padx=(10, 2))
        self.v_role = tk.StringVar(value=cfg.role)
        rb = ttk.Combobox(ctl, textvariable=self.v_role, width=7,
                          values=("peer", "master", "slave"), state="readonly")
        rb.pack(side="left")
        rb.bind("<<ComboboxSelected>>",
                lambda e: setattr(self.cfg, "role", self.v_role.get()))

        # ---- devices
        dev = tk.Frame(outer, bg=PANEL)
        dev.pack(anchor="w", padx=10, pady=2)
        self.v_in = tk.StringVar()
        self.v_out = tk.StringVar()
        tk.Label(dev, text="IN", bg=PANEL, fg=DIM, font=FONT).pack(side="left")
        self.cb_in = ttk.Combobox(dev, textvariable=self.v_in, width=28)
        self.cb_in.pack(side="left", padx=(2, 8))
        tk.Label(dev, text="OUT", bg=PANEL, fg=DIM, font=FONT).pack(side="left")
        self.cb_out = ttk.Combobox(dev, textvariable=self.v_out, width=28)
        self.cb_out.pack(side="left", padx=2)
        tk.Button(dev, text="Restart audio", command=self._restart_audio,
                  bg=EDGE, fg=FG, font=FONT, relief="flat"
                  ).pack(side="left", padx=8)
        self._fill_devices()

        # ---- hostif + log
        info = self.hostif.info if self.hostif else "hostif: none"
        tk.Label(outer, text=f"hostif: {info}", bg=PANEL, fg=CYAN,
                 font=FONT).pack(anchor="w", padx=10)
        self.logbox = tk.Text(outer, height=6, bg="#0b0d10", fg=FG,
                              font=("DejaVu Sans Mono", 8), relief="flat")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.logbox.configure(state="disabled")

        self.root.after(120, self._poll)

    # ------------------------------------------------------------ handlers
    def log(self, *parts):
        line = " ".join(str(p) for p in parts)
        self._loglines.append(line)

    def _set_mode(self):
        self.cfg.mode = self.v_mode.get()
        self.log(f"mode -> {self.cfg.mode}")

    def _set_flags(self):
        self.cfg.fec = self.v_fec.get()
        self.cfg.mpx = self.v_mpx.get()
        self.log(f"fec={self.cfg.fec} mpx={self.cfg.mpx}")

    def _fill_devices(self):
        try:
            from . import audio as A
            if not A.HAVE_SD:
                return
            import sounddevice as sd
            devs = sd.query_devices()
            ins = [f"{i}: {d['name']}" for i, d in enumerate(devs)
                   if d["max_input_channels"] > 0]
            outs = [f"{i}: {d['name']}" for i, d in enumerate(devs)
                    if d["max_output_channels"] > 0]
            self.cb_in["values"] = ins
            self.cb_out["values"] = outs
        except Exception as e:
            self.log(f"device query failed: {e}")

    def _restart_audio(self):
        if not self.audio:
            self.log("no audio backend (selftest / sounddevice missing)")
            return
        try:
            self.audio.stop()
            for var, attr in ((self.v_in, "in_device"),
                              (self.v_out, "out_device")):
                s = var.get().strip()
                if s:
                    setattr(self.cfg, attr, int(s.split(":")[0]))
            self.audio.start()
        except Exception as e:
            self.log(f"audio restart failed: {e}")

    # -------------------------------------------------------------- polling
    def _poll(self):
        try:
            m = self.modem.metrics()
            now = time.monotonic()
            blink = lambda t: (now - t) < 0.30
            self.led["TX"].set(blink(m["t_tx"]))
            self.led["RX"].set(blink(m["t_rx"]))
            self.led["ACK"].set(blink(m["t_ack"]))
            self.led["ERR"].set(blink(m["t_err"]))
            self.led["LINK"].set(m["link"])
            lanes = m["lanes"]
            self.led["SYNC L"].set(bool(lanes) and lanes[0]["sync"])
            self.led["SYNC R"].set(len(lanes) > 1 and lanes[1]["sync"])
            self.bar_snr_l.set(lanes[0]["snr"] if lanes else 0)
            self.bar_snr_r.set(lanes[1]["snr"] if len(lanes) > 1 else 0)
            self.bar_lvl.set(m["level_in_db"])
            self.lbl_stats.config(text=(
                f"TX {m['tx_kbps']:6.1f} kbit/s   RX {m['rx_kbps']:6.1f} kbit/s   "
                f"mode {m['mode']:>5}\n"
                f"drift {lanes[0].get('drift_line_ppm', lanes[0].get('drift_ppm', 0.0)):+6.1f} ppm   peer SNR "
                f"{m['peer_snr']:4.1f} dB   retx {m['n_retx']}   "
                f"crc {m['n_crc']}   q {m['qlen']}"))
            if lanes:
                self.mpx_l.set(lanes[0]["snr_lo"], lanes[0]["snr_hi"])
            if len(lanes) > 1:
                self.mpx_r.set(lanes[1]["snr_lo"], lanes[1]["snr_hi"])
            self._draw_const()
            if self._loglines:
                self.logbox.configure(state="normal")
                for line in self._loglines:
                    self.logbox.insert("end", line + "\n")
                self._loglines.clear()
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except Exception:
            pass
        self.root.after(120, self._poll)

    def _draw_const(self):
        pts = self.modem.const_points(0, 300)
        self.const.delete("pt")
        s = 170 / 4.0                       # +-2 full scale
        for z in pts:
            x = 85 + z.real * s
            y = 85 - z.imag * s
            if 0 <= x <= 170 and 0 <= y <= 170:
                self.const.create_rectangle(x, y, x + 1.6, y + 1.6,
                                            fill=GREEN, width=0, tags="pt")

    def _quit(self):
        if self.on_quit:
            self.on_quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
