# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Three utilities for working with SDR hardware via SoapySDR:
- `spectrum.py` — real-time CLI spectrum analyzer
- `spectrum_gui.py` — full PyQt5 GUI spectrum analyzer with waterfall, I/Q correction, and demodulator
- `reset_usb.py` — hardware USB reset via Linux ioctl (useful if a device becomes unresponsive)

**Supported hardware (`multi-hardware` branch):**

| Driver | Device | Sample rates | Gain | Freq range |
|--------|--------|-------------|------|------------|
| `miri` | SDRplay RSP1 (MSI001/MSI2500) | 2.048–8 MSps | 0–60 dB | 100 kHz–2 GHz |
| `rtlsdr` | RTL-SDR (RTL2832U) | 1.024–3.2 MSps | 0–50 dB | 24–1766 MHz |
| `plutosdr` | ADALM Pluto (PlutoSDR) | 2.5–20 MSps | 0–73 dB | 325 MHz–3.8 GHz |
| `lime` | LimeSDR Mini | 2–30.72 MSps | 0–70 dB | 10 MHz–3.5 GHz |

Primary test hardware: `Bus 001 Device 004: ID 1df7:2500 SDRplay RSP1`, SoapySDR driver `miri`.

## Branch strategy

- `master` — stable, SDRplay RSP1 only
- `multi-hardware` — adds RTL-SDR, ADALM Pluto, LimeSDR Mini via `HARDWARE_PROFILES`

## spectrum_gui.py

Full GUI spectrum analyzer with waterfall, I/Q correction, crosshair, and analog/digital demodulator with audio output.

Dependencies: `python3-pyqt5`, `python3-soapysdr`, `numpy`, `matplotlib`, `scipy`, `sounddevice` (pip), `libportaudio2` (apt).

```bash
python3 spectrum_gui.py
```

### Architecture

**Threading model:**
- Qt main thread owns all GUI and matplotlib canvas updates; uses `draw_idle()` (never `draw()`) so renders are non-blocking and don't starve the worker
- Worker runs as a `QThread` subclass (`_WorkerThread`) — **must be QThread, not `threading.Thread`**: the miri driver uses libusb which registers Qt `QSocketNotifier` objects; doing this from a plain Python thread causes heap corruption and a crash
- **`Radio()` is constructed inside `_worker_body()`**, not in the main thread — this eliminates the `QSocketNotifier` warning; `_do_start()` only stores pending parameters (`_pending_driver`, `_pending_freq`, `_pending_srate`, `_pending_gain`, `_pending_gain_max`, `_pending_max_chunk`) and starts the thread
- `queue.Queue(maxsize=2)` passes results to the main thread; oldest frame dropped if consumer is slow
- Worker never touches Qt objects — errors and calibration results are sent via the queue as tagged tuples: `("error", msg)`, `("calibration", alpha, phi, irr)`
- `QTimer` at 50 ms polls the queue and redraws (`_tick`)
- On stop: `radio.deactivate()` is called *before* `thread.wait()` so the blocking `readStream` returns immediately rather than waiting out the 2 s timeout

**Hardware profiles (`HARDWARE_PROFILES`):**
- Dict keyed by SoapySDR driver name: `"miri"`, `"rtlsdr"`, `"plutosdr"`, `"lime"`
- Each profile contains: `label`, `srates` (list), `srate_labels`, `gain_min`, `gain_max`, `freq_min`, `freq_max`, `max_chunk`
- `max_chunk` — max samples per `readStream` call; miri has a hard driver limit of 4096; all others use 131072
- `_enumerate_available_drivers()` — probes each driver at startup; returns list of connected driver keys
- Hardware combo in sidebar shows `●` for connected devices, `○` for absent; selecting hardware repopulates the sample-rate combo and updates the gain slider range
- Hardware, sample-rate, and FFT combos are all locked while the radio is running
- `Radio()` accepts `gain_max` and `max_chunk` so gain clipping and read chunking are hardware-correct

**DSP pipeline (worker thread):**
1. `radio.read(n)` — guaranteed-n-samples blocking read from SoapySDR stream; chunks internally at `radio.max_chunk` to respect per-driver limits
2. `iq_correct(samples, alpha, phi, dc_remove)` — optional DC removal + I/Q amplitude/phase correction
3. `_psd_db(samples, n)` — Blackman window → FFT → fftshift → dBFS; called `avg` times and averaged
4. Result enqueued as `(psd_float32, center_hz, srate, gain)`

**Plot layout:**
- Spectrum and waterfall are in **two separate `matplotlib.Figure` objects**, each with its own `FigureCanvas`, placed in a `QSplitter(Vertical)` — the boundary is user-draggable
- X-axis sync (replacing `sharex`) done via `xlim_changed` callbacks with a re-entrancy guard (`_xlim_syncing`) — pan/zoom in either panel syncs both; callbacks do **not** call `draw_idle()` to avoid recursion
- Sidebar is in a `QScrollArea` inside a `QSplitter(Horizontal)` with `setMinimumWidth`/`setMaximumWidth` — both splitters are user-resizable
- Waterfall: `imshow` on a `(rows × n)` float32 ring buffer, scrolled with `np.roll(..., axis=0)` each frame
- Auto Y: `ymax = max(psd) + 10 dB`, span preserved; updates spectrum Y limits and waterfall `clim`

**Key classes and functions:**
- `Radio(driver, freq, srate, gain, gain_max, max_chunk)` — wraps `SoapySDR.Device(f"driver={driver}")`; `enumerate` and `Device()` both use the string kwarg form (dict form unreliable in this SWIG binding); `read(n)` allocates a fresh contiguous buffer per `readStream` call using `self.max_chunk` — never passes numpy slice views, which cause SWIG/libusb memory corruption; `deactivate()` and `close()` are separate so the stream can be unblocked before the thread exits
- `iq_correct(samples, alpha, phi_rad, dc_remove)` — module-level function; corrects Q channel: `Q_c = (Q − I·sin φ) / (α·cos φ)`
- `estimate_iq_imbalance(samples)` — estimates α and φ from second-order statistics: `α = √(E[Q²]/E[I²])`, `φ = arcsin(E[I·Q] / √(E[I²]·E[Q²]))`; uses 16× FFT-size sample block
- `_irr_db(alpha, phi)` — Image Rejection Ratio: `10·log10((1+α²+2α·cos φ)/(1+α²−2α·cos φ))`

**Crosshair:**
- Yellow dashed `axvline` artists (`_xh_v1`, `_xh_v2`) on both spectrum and waterfall axes; follow mouse via `motion_notify_event`, hidden on `axes_leave_event`
- Frequency annotation (`_xh_txt`) on spectrum axis + sidebar label (`_cursor_lbl`)
- Left-click sets the demodulator center frequency and repositions the cyan demod marker

**Demodulator:**
- Sidebar controls: demod freq display (cyan), cursor freq (yellow), mode combo (WFM/NFM/AM/USB/LSB/CW), BW readout, volume slider, squelch slider, signal level `QProgressBar`, Start/Stop + Clear buttons
- `AUDIO_OK` flag: if `sounddevice` or PortAudio is unavailable, Start button is disabled gracefully
- `_DemodThread(QThread)` — second QThread for audio; `sig_level = pyqtSignal(float)` sends signal strength to main thread
- Worker feeds every corrected IQ frame into `_demod_q` (`queue.Queue(maxsize=64)`); spectrum and demod pipelines never block each other
- **IQ accumulation**: demod body collects ~25 ms of IQ (`ACCUM_SECS=0.025`, rounded to a power-of-two based on `radio.srate`) before the pre-decimation and demodulation steps
- **Pre-decimation to standard IF rate**: after accumulation, `_resample_iq()` brings the IQ block from the hardware sample rate down to a fixed per-mode intermediate rate (`DEMOD_IF_RATES`) before passing to `run_demod` — the demodulator always sees the same rate regardless of hardware
- `_demod_body()` uses `sounddevice.OutputStream` blocking write with `WRITE_CHUNK=2048`
- Cyan `axvline` demod marker + `axvspan` BW indicator on both axes; `axvspan` can't be moved so it's `.remove()`d and recreated at new position each click (`_update_demod_marker()`)
- USB span extends right of center; LSB extends left; all others are centered

**Standard IF rates (`DEMOD_IF_RATES`):**
```python
DEMOD_IF_RATES = {
    "WFM": 256_000,   # > 200 kHz channel; preserves stereo pilot at 38 kHz
    "NFM":  48_000,   # > 12.5 kHz channel
    "AM":   48_000,   # > 10 kHz channel; resample to AUDIO_RATE is then 1:1
    "USB":  24_000,   # > 3 kHz audio BW
    "LSB":  24_000,
    "CW":   12_000,   # > 500 Hz BW
}
```

**Demodulation continuity (`_DemodState`):**
- Created once per demod session; reset on mode or frequency change (>1 kHz)
- `downconv_phase` — accumulated sample index for the mixing tone in `_downconvert`; prevents phase reset to 0 at every IQ block boundary (primary cause of choppy WFM)
- `last_iq` — last decimated IQ sample from previous frame; prepended in `demod_fm` so the FM discriminator computes the inter-block phase difference correctly
- `bfo_phase` — accumulated phase for CW BFO tone (same issue as downconv)
- `zi` dict — `lfilter` initial conditions for every FIR/IIR stage; keyed by name (e.g. `"fm_ch_i"`, `"fm_deemph"`, `"am_audio"`); eliminates filter startup transients at block boundaries

**Demodulation DSP (module-level functions):**
- `_fir_cache` dict keyed by `(cutoff_hz, srate, ntaps)` — avoids recomputing FIR filters each frame
- `_lpf(cutoff_hz, srate, ntaps=127)` — `scipy.signal.firwin` lowpass
- `_apply_lpf(iq, cutoff_hz, srate, state, key)` — FIR lowpass on complex IQ; optional `state`/`key` for zi continuity
- `_apply_iir(b, a, x, state, key)` — IIR/FIR on real signal; optional `state`/`key` for zi continuity
- `_resample(audio, from_rate, to_rate)` — `scipy.signal.resample_poly` with GCD reduction, for real audio
- `_resample_iq(iq, from_rate, to_rate)` — resamples complex IQ by applying `resample_poly` to I and Q separately; scipy's Kaiser anti-aliasing filter prevents aliasing on decimation
- `_downconvert(iq, offset_hz, srate, state)` — shift baseband by `offset_hz`; `state` keeps phasor phase continuous across blocks
- `demod_fm(iq, srate, channel_bw, audio_bw, deemph_tau, state)` — FM discriminator + optional de-emphasis IIR
- `demod_am(iq, srate, channel_bw, audio_bw, state)` — envelope detection + single-pole DC block
- `demod_ssb(iq, srate, audio_bw, mode, state)` — USB takes real part directly; LSB conjugates first
- `demod_cw(iq, srate, bfo=700, state)` — mixes with 700 Hz BFO tone then AM-detects
- `run_demod(iq, srate, mode, state)` — dispatcher; looks up `DEMOD_MODES` dict for channel/audio BW and de-emphasis τ

**Demodulation modes (`DEMOD_MODES` dict):**
```python
DEMOD_MODES = {
    "WFM": (200_000, 15_000, 75e-6, "Wideband FM"),
    "NFM": ( 12_500,  3_000,     0, "Narrowband FM"),
    "AM":  ( 10_000,  5_000,     0, "AM"),
    "USB": (  3_000,  3_000,     0, "Upper Sideband"),
    "LSB": (  3_000,  3_000,     0, "Lower Sideband"),
    "CW":  (    500,    500,     0, "CW / Morse"),
}
```

### I/Q Correction

The MSI001/MSI2500 has amplitude and phase mismatch between I and Q channels, creating mirror images of signals. Three-stage correction:
1. **DC offset removal** — `samples -= mean(samples)`; eliminates the DC spike at center frequency
2. **Amplitude correction** (α) — scales Q to match I power; α=1.0 is ideal
3. **Phase correction** (φ) — restores 90° orthogonality between I and Q

"Auto Calibrate" collects a large sample block in the worker thread, estimates α/φ, and sends results back via the queue — no blocking of the UI thread.

## spectrum.py

Lightweight CLI version. Dependencies: `python3-soapysdr`, `numpy`, `matplotlib`.

```bash
python3 spectrum.py                          # 100 MHz, 2.048 MSps, 30 dB gain
python3 spectrum.py -f 433.92e6 -g 40       # 433 MHz ISM band
python3 spectrum.py -f 88e6 -s 8000000      # FM band, wider view
python3 spectrum.py --help
```

Key options: `-f FREQ`, `-s SRATE`, `-g GAIN`, `-n FFT_SIZE`, `--avg N`, `--ymin/--ymax`.

Keyboard controls while running: `↑↓` tune ±100 kHz, `PgUp/Dn` ±1 MHz, `+/-` gain ±5 dB, `r` reset peak hold, `q` quit.

### Architecture

- `Radio` — same SoapySDR wrapper as GUI version
- `SpectrumPlot` — owns the matplotlib figure and `FuncAnimation`; `_averaged_psd()` reads `avg` frames, applies Blackman window, FFTs, averages then converts to dBFS
- Peak hold maintained as a float32 array updated via `np.maximum` each frame

## reset_usb.py

```bash
sudo python3 reset_usb.py <BUS> <DEVICE>
# lsusb to find bus/device numbers
sudo python3 reset_usb.py 001 004   # RSP1 example
```

Sends `USBDEVFS_RESET` (ioctl 21780) to `/dev/bus/usb/{BUS}/{DEVICE}`. Requires root. Use if the device disappears from SoapySDR enumeration without being physically unplugged.
