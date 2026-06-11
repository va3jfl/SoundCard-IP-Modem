"""Host interfaces: how IP packets get in and out of the modem.

    pty     POSIX pseudo-terminal.  Prints /dev/pts/N; attach with
            `slattach` (SLIP) or `kissattach` (KISS) to get a network
            interface.  This is the "virtual COM port" on Linux/macOS.
    tcp     TCP server speaking KISS (Direwolf-style) or SLIP framing.
    tun     Linux TUN device: a real IP interface, no helper needed
            (requires root or CAP_NET_ADMIN).
    serial  A real or virtual serial port via pyserial (Windows com0com:
            give one end to the modem, the other to the OS/your app).

All interfaces deliver packets to modem.host_packet_in() and accept
packets from the link layer via .inject().
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading

# ----------------------------------------------------------- SLIP (RFC1055)
SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


def slip_encode(pkt: bytes) -> bytes:
    out = bytearray([SLIP_END])
    for b in pkt:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data: bytes):
        pkts = []
        for b in data:
            if self.esc:
                self.buf.append(SLIP_END if b == SLIP_ESC_END
                                else SLIP_ESC if b == SLIP_ESC_ESC else b)
                self.esc = False
            elif b == SLIP_ESC:
                self.esc = True
            elif b == SLIP_END:
                if self.buf:
                    pkts.append(bytes(self.buf))
                    self.buf.clear()
            else:
                self.buf.append(b)
            if len(self.buf) > 4096:
                self.buf.clear()
        return pkts


# ------------------------------------------------------------------- KISS
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def kiss_encode(pkt: bytes, port: int = 0) -> bytes:
    out = bytearray([FEND, (port << 4) & 0xF0])    # cmd 0 = data
    for b in pkt:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class KissDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.esc = False
        self.in_frame = False

    def feed(self, data: bytes):
        pkts = []
        for b in data:
            if b == FEND:
                if self.in_frame and len(self.buf) > 1:
                    if (self.buf[0] & 0x0F) == 0:      # data frame
                        pkts.append(bytes(self.buf[1:]))
                self.buf.clear()
                self.esc = False
                self.in_frame = True
            elif self.in_frame:
                if self.esc:
                    self.buf.append(FEND if b == TFEND
                                    else FESC if b == TFESC else b)
                    self.esc = False
                elif b == FESC:
                    self.esc = True
                else:
                    self.buf.append(b)
                if len(self.buf) > 4096:
                    self.buf.clear()
                    self.in_frame = False
        return pkts


def _codec(framing: str):
    if framing == "kiss":
        return kiss_encode, KissDecoder()
    if framing == "slip":
        return slip_encode, SlipDecoder()
    return (lambda p: p), None                      # raw


# -------------------------------------------------------------- base class
class HostIF:
    def __init__(self, cfg, modem, log=print):
        self.cfg = cfg
        self.modem = modem
        self.log = log
        self._stop = threading.Event()
        self.info = ""

    def start(self):                                # pragma: no cover
        raise NotImplementedError

    def stop(self):
        self._stop.set()

    def inject(self, pkt: bytes):                   # modem -> host
        raise NotImplementedError


# --------------------------------------------------------------------- PTY
class PtyIF(HostIF):
    def start(self):
        if sys.platform == "win32":
            raise RuntimeError("pty is POSIX-only; on Windows use "
                               "--hostif serial with a com0com pair")
        self.master, slave = os.openpty()
        import tty
        try:
            tty.setraw(self.master)
            tty.setraw(slave)
        except Exception:
            pass
        os.set_blocking(self.master, True)
        self.slave_name = os.ttyname(slave)
        self.enc, self.dec = _codec(self.cfg.framing)
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        self.info = f"pty {self.slave_name} ({self.cfg.framing})"
        self.log(f"host interface: {self.info}")
        if self.cfg.framing == "slip":
            self.log(f"  attach:  sudo slattach -L -p slip {self.slave_name}")
            self.log("           sudo ifconfig sl0 10.0.0.1 pointopoint "
                     f"10.0.0.2 mtu {self.cfg.mtu_hint} up")
        elif self.cfg.framing == "kiss":
            self.log(f"  attach:  sudo kissattach {self.slave_name} <port> <ip>")

    def _reader(self):
        while not self._stop.is_set():
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                continue
            if self.dec is None:
                self.modem.host_packet_in(data)
            else:
                for pkt in self.dec.feed(data):
                    self.modem.host_packet_in(pkt)

    def inject(self, pkt: bytes):
        try:
            os.write(self.master, self.enc(pkt))
        except OSError:
            pass


# --------------------------------------------------------------- TCP server
class TcpIF(HostIF):
    def start(self):
        self.enc, _ = _codec(self.cfg.framing if self.cfg.framing != "raw"
                             else "kiss")
        self.client = None
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.cfg.tcp_port))
        self.srv.listen(1)
        threading.Thread(target=self._accept, daemon=True).start()
        self.info = f"tcp 127.0.0.1:{self.cfg.tcp_port} ({self.cfg.framing})"
        self.log(f"host interface: {self.info}  (e.g. a KISS TNC client)")

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, addr = self.srv.accept()
            except OSError:
                break
            self.log(f"tcp client {addr[0]}:{addr[1]} connected")
            self.client = c
            dec = (KissDecoder() if self.cfg.framing != "slip"
                   else SlipDecoder())
            try:
                while not self._stop.is_set():
                    data = c.recv(4096)
                    if not data:
                        break
                    for pkt in dec.feed(data):
                        self.modem.host_packet_in(pkt)
            except OSError:
                pass
            self.client = None
            self.log("tcp client disconnected")

    def inject(self, pkt: bytes):
        c = self.client
        if c:
            try:
                c.sendall(self.enc(pkt))
            except OSError:
                pass


# --------------------------------------------------------------------- TUN
class TunIF(HostIF):
    TUNSETIFF = 0x400454CA
    IFF_TUN = 0x0001
    IFF_NO_PI = 0x1000

    def start(self):
        if not sys.platform.startswith("linux"):
            raise RuntimeError("tun is Linux-only")
        import fcntl
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        ifr = struct.pack("16sH", b"amodem%d", self.IFF_TUN | self.IFF_NO_PI)
        ifr = fcntl.ioctl(self.fd, self.TUNSETIFF, ifr)
        self.name = ifr[:16].rstrip(b"\0").decode()
        threading.Thread(target=self._reader, daemon=True).start()
        self.info = f"tun {self.name}"
        self.log(f"host interface: {self.info}")
        if self.cfg.tun_ip:
            import subprocess
            for cmd in ((["ip", "addr", "add", self.cfg.tun_ip,
                          "peer", self.cfg.tun_peer, "dev", self.name]
                         if self.cfg.tun_peer else
                         ["ip", "addr", "add", self.cfg.tun_ip,
                          "dev", self.name]),
                        ["ip", "link", "set", self.name, "up",
                         "mtu", str(self.cfg.mtu_hint)]):
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                except Exception as e:
                    self.log(f"  (run manually) {' '.join(cmd)}: {e}")
            self.log(f"  {self.name}: {self.cfg.tun_ip} "
                     f"peer {self.cfg.tun_peer} mtu {self.cfg.mtu_hint}")

    def _reader(self):
        while not self._stop.is_set():
            try:
                pkt = os.read(self.fd, 4096)
            except OSError:
                break
            if pkt:
                self.modem.host_packet_in(pkt)

    def inject(self, pkt: bytes):
        try:
            os.write(self.fd, pkt)
        except OSError:
            pass


# ------------------------------------------------------------------ serial
class SerialIF(HostIF):
    def start(self):
        try:
            import serial
        except ImportError:
            raise RuntimeError("pyserial required: pip install pyserial")
        if not self.cfg.serial_port:
            raise RuntimeError("--serial-port required (e.g. COM5)")
        self.ser = serial.Serial(self.cfg.serial_port, 115200, timeout=0.1)
        self.enc, self.dec = _codec(self.cfg.framing)
        threading.Thread(target=self._reader, daemon=True).start()
        self.info = f"serial {self.cfg.serial_port} ({self.cfg.framing})"
        self.log(f"host interface: {self.info}")

    def _reader(self):
        while not self._stop.is_set():
            try:
                data = self.ser.read(4096)
            except Exception:
                break
            if not data:
                continue
            if self.dec is None:
                self.modem.host_packet_in(data)
            else:
                for pkt in self.dec.feed(data):
                    self.modem.host_packet_in(pkt)

    def inject(self, pkt: bytes):
        try:
            self.ser.write(self.enc(pkt))
        except Exception:
            pass


def make_hostif(cfg, modem, log=print) -> HostIF | None:
    kind = cfg.hostif
    if kind == "none":
        return None
    cls = {"pty": PtyIF, "tcp": TcpIF, "tun": TunIF, "serial": SerialIF}.get(kind)
    if cls is None:
        raise ValueError(f"unknown hostif '{kind}'")
    h = cls(cfg, modem, log)
    h.start()
    modem.attach_host(h.inject)
    return h
