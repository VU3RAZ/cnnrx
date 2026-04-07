# cnnrx — SDR Spectrum Analyzer & Demodulator

A real-time spectrum analyzer and demodulator for multiple SDR devices, built with Python, SoapySDR, and PyQt5.

---

## Supported Hardware

| Device | SoapySDR driver | Sample rates | Gain | Frequency range |
|--------|----------------|-------------|------|----------------|
| SDRplay RSP1 (MSI001/MSI2500) | `miri` | 2.048 · 2.496 · 4 · 6 · 8 MSps | 0–60 dB | 100 kHz – 2 GHz |
| RTL-SDR (RTL2832U) | `rtlsdr` | 1.024 · 1.536 · 1.92 · 2.048 · 2.4 · 3.2 MSps | 0–50 dB | 24 – 1766 MHz |
| ADALM Pluto (PlutoSDR) | `plutosdr` | 2.5 · 4 · 8 · 16 · 20 MSps | 0–73 dB | 325 MHz – 3.8 GHz |
| LimeSDR Mini | `lime` | 2 · 4 · 8 · 16 · 30.72 MSps | 0–70 dB | 10 MHz – 3.5 GHz |

Connected devices are auto-detected at startup and shown with **●** in the hardware selector. Undetected devices are shown with **○** and can still be selected (will error on Start if not present).

---

## Features

- Real-time spectrum display with peak hold
- Scrolling waterfall display
- I/Q amplitude and phase correction (manual + auto-calibrate)
- Analog demodulator: **WFM · NFM · AM · USB · LSB · CW**
- Mouse crosshair with frequency readout
- Click-to-tune demodulator center frequency
- Resizable spectrum / waterfall / sidebar panels
- CLI version for headless use

---

## Branches

| Branch | Description |
|--------|-------------|
| `master` | Stable — SDRplay RSP1 only |
| `multi-hardware` | Adds RTL-SDR, ADALM Pluto, LimeSDR Mini |

---

## Installation

### System packages

```bash
sudo apt install python3-pyqt5 python3-soapysdr libportaudio2
```

For RTL-SDR:
```bash
sudo apt install soapysdr-module-rtlsdr
```

For ADALM Pluto:
```bash
sudo apt install soapysdr-module-plutosdr
```

For LimeSDR Mini:
```bash
sudo apt install soapysdr-module-lms7
```

### Python packages

```bash
pip install -r requirements.txt
```

`requirements.txt`:
```
numpy>=1.24
matplotlib>=3.7
scipy>=1.10
PyQt5>=5.15
sounddevice>=0.4
```

---

## Usage

### GUI

```bash
python3 spectrum_gui.py
```

1. Select your hardware from the **Hardware** dropdown — connected devices are marked **●**
2. Set center frequency, sample rate, and gain
3. Press **▶ Start** — spectrum and waterfall begin updating
4. Move the mouse over either plot to read frequency on the crosshair
5. Left-click to lock the demodulator to that frequency
6. Select mode (WFM/NFM/AM/USB/LSB/CW), adjust volume and squelch, press **▶ Demod**
7. Use **Auto Calibrate** to measure and correct I/Q amplitude/phase imbalance (SDRplay RSP1)

### CLI

```bash
python3 spectrum.py                          # 100 MHz, 2.048 MSps, 30 dB gain
python3 spectrum.py -f 88e6 -s 8000000      # FM broadcast band
python3 spectrum.py -f 433.92e6 -g 40       # 433 MHz ISM band
python3 spectrum.py --help
```

Keyboard controls while running:

| Key | Action |
|-----|--------|
| `↑` / `↓` | Tune ±100 kHz |
| `PgUp` / `PgDn` | Tune ±1 MHz |
| `+` / `-` | Gain ±5 dB |
| `r` | Reset peak hold |
| `q` | Quit |

### USB reset (if device disappears)

```bash
lsusb                              # find bus and device numbers
sudo python3 reset_usb.py 001 004  # adjust to match your device
```

---

## Demodulation Modes

| Mode | Channel BW | Audio BW | Notes |
|------|-----------|----------|-------|
| WFM | 200 kHz | 15 kHz | Broadcast FM, 75 µs de-emphasis |
| NFM | 12.5 kHz | 3 kHz | Amateur / PMR narrowband FM |
| AM | 10 kHz | 5 kHz | Envelope detection |
| USB | 3 kHz | 3 kHz | Upper sideband SSB |
| LSB | 3 kHz | 3 kHz | Lower sideband SSB |
| CW | 500 Hz | 500 Hz | Morse, 700 Hz BFO |

---

## Architecture Notes

- **`Radio()` is opened inside `_WorkerThread(QThread)`** — the miri/libusb driver registers Qt `QSocketNotifier` objects; opening from the main thread triggers a warning and can cause instability
- **`HARDWARE_PROFILES`** dict stores per-device sample rates, gain range, frequency limits, and `max_chunk` (max samples per `readStream` call — the miri driver has a hard limit of 4096; all others use 131072)
- **`_DemodState`** carries phase and filter `zi` state across IQ blocks — prevents the mixing-tone phase reset, FM discriminator gap, and IIR/FIR startup transients that otherwise cause choppy audio
- **IQ accumulation** (~25 ms) before calling `run_demod` reduces `resample_poly` call rate ~40× and keeps audio smooth at all sample rates
- **`draw_idle()`** (not `draw()`) is used for all canvas updates so the Qt event loop never blocks the worker thread during renders
- Spectrum and waterfall are in separate `matplotlib.Figure` objects inside a `QSplitter(Vertical)`; X-axis sync is done via `xlim_changed` callbacks with a re-entrancy guard

---

## License

MIT
