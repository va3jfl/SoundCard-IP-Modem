"""Command line entry point.

    python3 -m audiomodem                       # UI + pty/slip
    python3 -m audiomodem --hostif tun          # real IP interface (root)
    python3 -m audiomodem --no-ui --mode 64qam
    python3 -m audiomodem --list-devices
    python3 -m audiomodem --selftest            # no soundcard needed
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import ModemConfig, MODES


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audiomodem",
        description="Soundcard OFDM modem: TCP/IP over an audio cable.")
    p.add_argument("--fs", type=int, default=48000,
                   choices=(48000, 96000, 192000),
                   help="sample rate: 48000 = compatible profile, "
                        "192000 = wideband speed profile (24-bit cards, "
                        "both ends must support it)")
    p.add_argument("--mode", default="auto",
                   choices=("auto",) + MODES,
                   help="constellation (auto adapts to measured SNR)")
    p.add_argument("--no-fec", action="store_true",
                   help="disable Reed-Solomon FEC")
    p.add_argument("--no-mpx", action="store_true",
                   help="disable the lo/hi subband multiplex interleave")
    p.add_argument("--no-bond", action="store_true",
                   help="use a single (mono) channel instead of stereo L+R")
    p.add_argument("--role", default="peer",
                   choices=("peer", "master", "slave"))
    p.add_argument("--hostif", default="pty",
                   choices=("pty", "tcp", "tun", "serial", "none"))
    p.add_argument("--framing", default=None, choices=("slip", "kiss", "raw"),
                   help="packet framing for pty/tcp/serial (default: slip "
                        "for pty/serial, kiss for tcp)")
    p.add_argument("--tcp-port", type=int, default=8001)
    p.add_argument("--serial-port", default="",
                   help="e.g. COM5 (Windows, com0com pair)")
    p.add_argument("--tun-ip", default="10.55.0.1/24")
    p.add_argument("--tun-peer", default="10.55.0.2")
    p.add_argument("--in-device", default=None,
                   help="sounddevice index or name substring")
    p.add_argument("--out-device", default=None)
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--no-ui", action="store_true", help="run headless")
    p.add_argument("--selftest", action="store_true",
                   help="software loopback test, no soundcard needed")
    p.add_argument("--check-resampler", action="store_true",
                   help=argparse.SUPPRESS)
    return p


def _dev(v):
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return v


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        from . import selftest
        return selftest.main()

    if args.list_devices:
        from . import audio
        print(audio.list_devices())
        return 0

    framing = args.framing or ("kiss" if args.hostif == "tcp" else "slip")
    cfg = ModemConfig(
        fs=args.fs,
        mode=args.mode, fec=not args.no_fec, mpx=not args.no_mpx,
        bond=not args.no_bond, role=args.role, hostif=args.hostif,
        framing=framing, tcp_port=args.tcp_port,
        serial_port=args.serial_port, tun_ip=args.tun_ip,
        tun_peer=args.tun_peer, in_device=_dev(args.in_device),
        out_device=_dev(args.out_device))

    from .modem import Modem
    from .hostif import make_hostif

    logbuf = []
    def log(*parts):
        line = " ".join(str(p) for p in parts)
        print(line, flush=True)
        logbuf.append(line)

    modem = Modem(cfg, log=log)
    hostif = None
    try:
        hostif = make_hostif(cfg, modem, log=log)
    except Exception as e:
        log(f"host interface failed: {e}")
        if args.no_ui:
            return 1

    audio_io = None
    try:
        from .audio import AudioIO
        audio_io = AudioIO(cfg, modem, log=log)
        audio_io.start()
    except Exception as e:
        log(f"audio unavailable: {e}")
        log("running without audio (UI/selftest still work)")

    def shutdown():
        if audio_io:
            audio_io.stop()
        if hostif:
            hostif.stop()

    if args.no_ui:
        log("headless; Ctrl-C to quit")
        try:
            while True:
                time.sleep(2.0)
                m = modem.metrics()
                log(f"link={'UP' if m['link'] else 'down'} "
                    f"snr={m['snr']:4.1f}dB tx={m['tx_kbps']:5.1f} "
                    f"rx={m['rx_kbps']:5.1f} kbit/s mode={m['mode']} "
                    f"retx={m['n_retx']} crc={m['n_crc']}")
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    try:
        from .ui import ModemUI
    except ImportError as e:
        log(f"UI unavailable ({e}); falling back to --no-ui. "
            "Install the python3-tk package for the front panel.")
        try:
            while True:
                time.sleep(2.0)
                m = modem.metrics()
                log(f"link={'UP' if m['link'] else 'down'} "
                    f"snr={m['snr']:4.1f}dB tx={m['tx_kbps']:5.1f} "
                    f"rx={m['rx_kbps']:5.1f} kbit/s mode={m['mode']} "
                    f"retx={m['n_retx']} crc={m['n_crc']}")
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    ui = ModemUI(cfg, modem, audio=audio_io, hostif=hostif, on_quit=shutdown)
    for line in logbuf:
        ui.log(line)
    # route future logs into the UI as well
    modem.log = ui.log
    modem.link.log = ui.log
    ui.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
