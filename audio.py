"""Full-duplex audio I/O via the sounddevice (PortAudio) library.

The import is guarded so the rest of the package (selftest, host
interfaces) works on machines without PortAudio installed.
"""
from __future__ import annotations

import sys

try:
    import sounddevice as sd
    HAVE_SD = True
except Exception as _e:                    # pragma: no cover
    sd = None
    HAVE_SD = False
    _IMPORT_ERR = _e


def require():
    if not HAVE_SD:
        raise RuntimeError(
            "the 'sounddevice' package (and the PortAudio library) is "
            f"required for real audio I/O: {_IMPORT_ERR}\n"
            "    pip install sounddevice")


def list_devices() -> str:
    require()
    return str(sd.query_devices())


class AudioIO:
    """Owns the duplex stream; pumps the modem from the audio callback."""

    def __init__(self, cfg, modem, log=print):
        require()
        self.cfg = cfg
        self.modem = modem
        self.log = log
        self.stream = None
        self._xruns = 0

    def _callback(self, indata, outdata, frames, t, status):
        if status:
            self._xruns += 1
            if self._xruns in (1, 10, 100):
                self.log(f"audio status: {status} (x{self._xruns})")
        try:
            blk = indata
            if blk.shape[1] == 1:
                blk = blk.repeat(2, axis=1)
            self.modem.push_rx(blk.astype('float32'))
            outdata[:] = self.modem.pull_tx(frames)
        except Exception as e:             # never let the callback die silently
            self.log(f"audio callback error: {e!r}")
            outdata.fill(0)

    def start(self):
        self.stream = sd.Stream(
            samplerate=self.cfg.fs,
            blocksize=self.cfg.blocksize,
            channels=2,
            dtype='float32',
            device=(self.cfg.in_device, self.cfg.out_device),
            callback=self._callback,
            latency='low')
        self.stream.start()
        di = sd.query_devices(self.stream.device[0])['name']
        do = sd.query_devices(self.stream.device[1])['name']
        self.log(f"audio running: in '{di}'  out '{do}'  "
                 f"fs={self.cfg.fs} block={self.cfg.blocksize}")

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
