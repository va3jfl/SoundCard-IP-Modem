# 📻 audiomodem 
**TCP/IP over a soundcard, up to ~2 Mbit/s**

> A software modem that turns two PCs' soundcards into a point-to-point network link. It modulates IP packets onto audio with OFDM (DMT), bonds the left and right stereo channels into two parallel lanes, protects everything with Reed-Solomon FEC and an ARQ with fast retransmit, and hands packets to the OS through various virtual interfaces. Written in pure Python + NumPy.

### 📸 Screenshots
*Click images to expand*

<p align="center">
  <a href="screenshot.jpg" target="_blank">
    <img src="screenshot.jpg" width="45%" alt="Audiomodem Graphical UI with Constellation Map" style="margin-right: 2%;">
  </a>
  <a href="screenshot2" target="_blank">
    <img src="screenshot2.jpg" width="45%" alt="CLI Selftest Output showing Goodput" style="margin-left: 2%;">
  </a>
</p>

---

### ✨ Key Features & Operating Profiles

* **Dual-Lane Bonding:** Utilizes both left and right stereo channels as parallel lanes to maximize throughput.
* **Virtual Interfaces:** Hands packets to the OS via a virtual serial port (SLIP/KISS), a TCP TNC socket, or a native Linux TUN interface.
* **Robust Error Correction:** Protected by Reed-Solomon FEC, an ARQ with fast retransmit, and adaptive serialization.
* **MPX Subband Interleave:** Gives frequency diversity across each lane's low/high halves. While it doesn't add base capacity (both halves share the same physical bandwidth), at 192k, that bandwidth genuinely extends all the way to ~87 kHz for massive gains.

### Available Profiles
1.  **Compatible Profile (`--fs 48000`)**
    * Carriers: 0.3–18 kHz
    * Hardware: Works on any standard duplex soundcard.
    * Speed: Up to 64-QAM, ~115 kbit/s each way.
2.  **Wideband Speed Profile (`--fs 192000`)**
    * Carriers: 3–87 kHz (fills all spectrum far above the audio band, similar to FM multiplexing).
    * Hardware: Requires modern 24-bit/192 kHz codecs on a direct cable.
    * Speed: Up to 4096-QAM, delivering **~1.02 Mbit/s each direction simultaneously (~2 Mbit/s aggregate)** at 50 dB SNR.

---

### 🚀 Quick Start

### 1. Installation
Install the required dependencies via pip and apt:
```bash
pip install numpy sounddevice         # sounddevice needs PortAudio
pip install pyserial                  # only required for --hostif serial
sudo apt install python3-tk           # only required for the UI (Linux)

Run the bundled regression suite:
Test 14 simulated-cable scenarios in ~90 seconds without a soundcard:

python -m audiomodem --selftest

### 2. Wiring and OS Setup

    Cabling: Connect two stereo 3.5 mm TRS cables, crossed. Each PC's line-out must go into the other PC's line-in. (Bonding uses both L and R, so do not use mono cables).

    OS Audio Configuration: * For the 192k profile, set both machines' playback and capture devices to 24-bit, 192000 Hz.
    * Windows: Sound Control Panel → Device → Advanced → Default Format.
    * Linux: ALSA usually follows requested rates automatically (PipeWire/Pulse users may need to allow 192 kHz in the daemon config).

        CRITICAL: Disable all audio "enhancements" on both ends (loudness EQ, noise suppression, echo cancellation, AGC, spatial sound). These will destroy the waveform.

    Volume: Set levels to ~80%. The RX level meter should sit around −20…−10 dBFS.


### 3. Usage (Linux, SLIP Example)

On PC 1:
python -m audiomodem --fs 192000
# log shows e.g.:  host interface: pty /dev/pts/4 (slip)

sudo slattach -L -p slip /dev/pts/4
sudo ifconfig sl0 10.0.0.1 pointopoint 10.0.0.2 mtu 1500 up

On PC 2:
Repeat the same commands with the IPs swapped. Wait for LINK, then you are ready to ping, ssh, or scp!

Other Interfaces: --hostif tun (real IP interface, needs root), --hostif tcp (KISS over TCP for TNC software), --hostif serial (Windows + com0com virtual COM pair), or --no-ui for headless operation.


### 📊 Measured Speeds

*(Bundled simulation, both directions at once)*

| Profile | Mode       | Required SNR | Goodput Per Direction |
| :------ | :--------- | :----------- | :-------------------- |
| 48 k    | QPSK       | ~10 dB       | 37 kbit/s             |
| 48 k    | 64-QAM     | ~25 dB       | 112–115 kbit/s        |
| 192 k   | 256-QAM    | ~31 dB       | ~300 kbit/s class     |
| 192 k   | 1024-QAM   | ~38 dB       | 786 kbit/s            |
| 192 k   | 4096-QAM   | ~44 dB       | 1019 kbit/s           |

Note: A real direct cable through decent onboard codecs typically measures 30–45 dB. Expect 256/1024-QAM as the standard operating point. --mode auto (default) climbs as measured SNR allows and backs off on retransmission pressure.

### ⚙️ How It Works (Technical Deep Dive)

    Modulation: DMT/OFDM, 512-pt FFT, 64-sample cyclic prefix.

    Carriers: 189 carriers to 18 kHz at 48k; 229 carriers (3–87.4 kHz) at 192k.

    Synchronization: 24 pilots track phase and timing, driving a closed-loop sample-rate-offset corrector (streaming 8× polyphase + windowed-sinc fractional resampler ahead of the demodulator). This nulls the sample-rate offset, allowing even 4096-QAM to survive ±300 ppm drifting clocks.

    Framing & Mapping: Schmidl-Cox sync, LS channel-estimation preamble, rep-3 QPSK header, up to 24 (48k) / 32 (192k) data symbols. Gray-mapped QPSK…4096-QAM per symbol.

    Data Integrity: RS(255,223) + interleaver + scrambler + CRC-32.

    Link Layer: Go-back-N (window 8/16) with piggybacked cumulative ACKs, duplicate-ACK fast retransmit, serialization-aware adaptive RTO, and a receive reorder buffer that absorbs bonded-lane skew.

### 🛠️ Troubleshooting

    No SYNC → Check cable directions and volumes (RX LEVEL must actively move).

    SYNC but no LINK → Check the reverse cable.

    ERR/retx storms → SNR is too low for the current mode, or an OS audio "enhancement" is silently mangling the audio.

    192k won't start → The codec or OS device format isn't correctly set to 192 kHz on both ends. Fall back to --fs 48000.

    Throughput is surprisingly low → Check both SYNC LEDs (ensuring bonding is active) and verify the constellation map looks like a distinct grid, not a blurry cloud.
