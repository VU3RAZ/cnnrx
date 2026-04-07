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

Connected devices are auto-detected at startup and shown with **●** in the hardware selector. Undetected devices are shown with **○**.

---

## Features

- Real-time spectrum display with peak hold
- Scrolling waterfall display
- Resizable spectrum / waterfall / sidebar panels (drag the dividers)
- I/Q amplitude and phase correction with auto-calibrate
- Analog demodulator: **WFM · NFM · AM · USB · LSB · CW**
- Pre-decimation to a fixed intermediate rate per mode — demodulator sees the same sample rate regardless of which hardware is in use
- Mouse crosshair with live frequency readout
- Click-to-tune demodulator center frequency
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

Additional SoapySDR modules for each device:

```bash
sudo apt install soapysdr-module-rtlsdr      # RTL-SDR
sudo apt install soapysdr-module-plutosdr    # ADALM Pluto
sudo apt install soapysdr-module-lms7        # LimeSDR Mini
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
7. Use **Auto Calibrate** to measure and correct I/Q imbalance (primarily for SDRplay RSP1)

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

The demodulator receives IQ pre-decimated to a fixed intermediate rate per mode, independent of hardware sample rate:

| Mode | IF rate | Channel BW | Audio BW | Notes |
|------|---------|-----------|----------|-------|
| WFM | 256 kHz | 200 kHz | 15 kHz | Broadcast FM, 75 µs de-emphasis, stereo pilot preserved |
| NFM | 48 kHz | 12.5 kHz | 3 kHz | Amateur / PMR narrowband FM |
| AM | 48 kHz | 10 kHz | 5 kHz | Envelope detection |
| USB | 24 kHz | 3 kHz | 3 kHz | Upper sideband SSB |
| LSB | 24 kHz | 3 kHz | 3 kHz | Lower sideband SSB |
| CW | 12 kHz | 500 Hz | 500 Hz | Morse, 700 Hz BFO |

---

## Architecture Notes

- **`Radio()` opened inside `_WorkerThread(QThread)`** — the miri/libusb driver registers Qt `QSocketNotifier` objects; opening from the main thread triggers warnings and can cause instability
- **`HARDWARE_PROFILES`** stores per-device sample rates, gain range, frequency limits, and `max_chunk` (max samples per `readStream` call — miri is limited to 4096; all others use 131072)
- **Pre-decimation to IF rate** — after accumulating ~25 ms of IQ, `_resample_iq()` brings the block from the hardware sample rate to the mode's fixed IF rate before `run_demod`; scipy's Kaiser anti-aliasing filter is applied automatically; the demod functions always see consistent input rates and filter coefficients across all hardware
- **`_DemodState`** carries mixing-tone phase, FM discriminator last sample, BFO phase, and all FIR/IIR filter `zi` states across IQ blocks — eliminates phase resets and filter transients at block boundaries
- **IQ accumulation** (~25 ms, power-of-two samples) reduces `resample_poly` call rate ~40× vs processing every small radio frame
- **`draw_idle()`** used for all canvas updates — Qt event loop never blocks the worker thread during renders
- Spectrum and waterfall in separate `matplotlib.Figure` objects inside a `QSplitter(Vertical)`; X-axis sync via `xlim_changed` callbacks with re-entrancy guard

---

## License

MIT
