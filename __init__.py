"""audiomodem: TCP/IP over a soundcard using a stereo-bonded OFDM (DMT) modem.

Quick start (two PCs, audio cables crossed both ways):

    python3 -m audiomodem                 # prints a /dev/pts/N to attach
    sudo slattach -L -p slip /dev/pts/N
    sudo ifconfig sl0 10.0.0.1 pointopoint 10.0.0.2 mtu 576 up

See README.md for details.
"""
__version__ = "2.0.0"

from .config import ModemConfig
from .modem import Modem
