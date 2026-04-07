#!/usr/bin/env python3
"""
SDR Spectrum Analyzer + Demodulator — PyQt5 GUI
Hardware: MSI001/MSI2500 (SDRplay RSP1) via SoapySDR miri driver.

Run:  python3 spectrum_gui.py
Deps: python3-pyqt5, python3-soapysdr, numpy, scipy, matplotlib, sounddevice
      sudo apt install libportaudio2
"""

import sys
import queue
import numpy as np
from math import gcd

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QSlider, QSpinBox, QDoubleSpinBox, QScrollArea,
    QStatusBar, QSizePolicy, QCheckBox, QProgressBar,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal

import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.ticker import EngFormatter

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
except ImportError:
    sys.exit("SoapySDR Python bindings not found.  sudo apt install python3-soapysdr")

try:
    import sounddevice as _sd
    AUDIO_OK = True
except (ImportError, OSError):
    AUDIO_OK = False

VALID_SRATES   = [2_048_000, 2_496_000, 4_000_000, 6_000_000, 8_000_000]
SRATE_LABELS   = ["2.048 MSps", "2.496 MSps", "4.000 MSps", "6.000 MSps", "8.000 MSps"]
WATERFALL_ROWS = 200
WF_CMAPS       = ["inferno", "plasma", "magma", "viridis", "turbo"]
MAX_READ_CHUNK = 4096   # miri driver max per readStream call
AUDIO_RATE     = 48_000

# Demodulator modes: name → (channel_bw_hz, audio_bw_hz, deemph_tau_s, label)
DEMOD_MODES = {
    "WFM": (200_000, 15_000, 75e-6, "Wideband FM"),
    "NFM": ( 12_500,  3_000,     0, "Narrowband FM"),
    "AM":  ( 10_000,  5_000,     0, "AM"),
    "USB": (  3_000,  3_000,     0, "Upper Sideband"),
    "LSB": (  3_000,  3_000,     0, "Lower Sideband"),
    "CW":  (    500,    500,     0, "CW / Morse"),
}

DARK   = "#111122"
PLOTBG = "#0a0a1a"
ACCENT = "#00dd88"
PEAK_C = "#ff4455"

STYLESHEET = """
QMainWindow, QWidget { background: #1a1a2e; color: #cccccc; }
QGroupBox {
    border: 1px solid #333355; border-radius: 4px;
    margin-top: 8px; font-weight: bold; color: #aaaacc;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #0f0f23; border: 1px solid #333355;
    border-radius: 3px; padding: 3px 6px; color: #dddddd;
}
QLineEdit:focus, QComboBox:focus { border-color: #5555aa; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px; border: 1px solid #555577;
    border-radius: 3px; background: #0f0f23;
}
QCheckBox::indicator:checked { background: #5555cc; border-color: #7777ee; }
QSlider::groove:horizontal { height: 4px; background: #333355; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    background: #5555cc; border-radius: 7px;
}
QSlider::sub-page:horizontal { background: #5555cc; border-radius: 2px; }
QPushButton {
    background: #252540; border: 1px solid #444466;
    border-radius: 4px; padding: 5px 10px; color: #ccccdd;
}
QPushButton:hover   { background: #333360; border-color: #6666aa; }
QPushButton:pressed { background: #1a1a3a; }
QPushButton#startBtn[running="false"] {
    background: #1a4a2a; border-color: #33aa55; color: #55ff88;
}
QPushButton#startBtn[running="true"] {
    background: #4a1a1a; border-color: #aa3333; color: #ff5555;
}
QPushButton#demodBtn[active="false"] {
    background: #1a2a4a; border-color: #3355aa; color: #55aaff;
}
QPushButton#demodBtn[active="true"] {
    background: #4a1a1a; border-color: #aa3333; color: #ff5555;
}
QProgressBar {
    background: #0f0f23; border: 1px solid #333355;
    border-radius: 3px; text-align: right; color: #aaaacc;
    padding: 1px;
}
QProgressBar::chunk { background: #336633; border-radius: 2px; }
QStatusBar { background: #0f0f1a; color: #888899; font-size: 11px; }
QScrollArea { border: none; }
"""


# ──────────────────────────────────────────────────────────────────────────────
# DSP: I/Q correction
# ──────────────────────────────────────────────────────────────────────────────

def iq_correct(samples, alpha, phi_rad, dc_remove):
    s = samples.astype(np.complex64)
    if dc_remove:
        s = s - np.mean(s)
    if alpha != 1.0 or phi_rad != 0.0:
        I = np.real(s)
        Q = np.imag(s)
        Q_c = (Q - I * np.sin(phi_rad)) / (alpha * np.cos(phi_rad))
        s = (I + 1j * Q_c).astype(np.complex64)
    return s


def estimate_iq_imbalance(samples):
    s = samples.astype(np.complex128) - np.mean(samples)
    I, Q = np.real(s), np.imag(s)
    P_II = float(np.mean(I * I))
    P_QQ = float(np.mean(Q * Q))
    P_IQ = float(np.mean(I * Q))
    if P_II < 1e-20 or P_QQ < 1e-20:
        return 1.0, 0.0, float("inf")
    alpha = float(np.sqrt(P_QQ / P_II))
    phi   = float(np.arcsin(np.clip(P_IQ / np.sqrt(P_II * P_QQ), -1.0, 1.0)))
    irr   = _irr_db(alpha, phi)
    return alpha, phi, irr


def _irr_db(alpha, phi_rad):
    num = 1.0 + alpha**2 + 2.0 * alpha * np.cos(phi_rad)
    den = 1.0 + alpha**2 - 2.0 * alpha * np.cos(phi_rad)
    return float("inf") if den < 1e-12 else 10.0 * np.log10(max(num / den, 1e-12))


# ──────────────────────────────────────────────────────────────────────────────
# DSP: Demodulation
# ──────────────────────────────────────────────────────────────────────────────

_fir_cache: dict = {}


def _lpf(cutoff_hz, srate, ntaps=127):
    from scipy.signal import firwin
    key = (int(cutoff_hz), int(srate), ntaps)
    if key not in _fir_cache:
        _fir_cache[key] = firwin(ntaps, cutoff_hz / (srate / 2),
                                 window="hamming").astype(np.float32)
    return _fir_cache[key]


class _DemodState:
    """Continuity state across demod frames; reset on mode/frequency change."""
    __slots__ = ("downconv_phase", "last_iq", "bfo_phase", "zi")

    def __init__(self):
        self.downconv_phase: float = 0.0   # accumulated sample index for mixing tone
        self.last_iq = None                # last decimated IQ sample for FM discriminator
        self.bfo_phase: float = 0.0        # accumulated sample index for CW BFO
        self.zi: dict = {}                 # filter states keyed by name


def _apply_lpf(iq, cutoff_hz, srate, ntaps=127, state=None, key="lpf"):
    """FIR lowpass on complex IQ; state keeps filter zi continuous across blocks."""
    from scipy.signal import lfilter, lfilter_zi
    h = _lpf(cutoff_hz, srate, ntaps).astype(np.float64)
    ri = np.real(iq).astype(np.float64)
    rq = np.imag(iq).astype(np.float64)
    if state is not None:
        ki, kq = key + "_i", key + "_q"
        if ki not in state.zi:
            z0 = lfilter_zi(h, 1.0)
            state.zi[ki] = z0 * ri[0]
            state.zi[kq] = z0 * rq[0]
        ri_f, state.zi[ki] = lfilter(h, 1.0, ri, zi=state.zi[ki])
        rq_f, state.zi[kq] = lfilter(h, 1.0, rq, zi=state.zi[kq])
    else:
        ri_f = lfilter(h, 1.0, ri)
        rq_f = lfilter(h, 1.0, rq)
    return (ri_f.astype(np.float32) + 1j * rq_f.astype(np.float32)).astype(np.complex64)


def _apply_iir(b, a, x, state=None, key="iir"):
    """IIR/FIR filter on real signal; state keeps zi continuous across blocks."""
    from scipy.signal import lfilter, lfilter_zi
    b = np.asarray(b, dtype=np.float64)
    a = np.asarray(a, dtype=np.float64)
    x = x.astype(np.float64)
    if state is not None:
        if key not in state.zi:
            state.zi[key] = lfilter_zi(b, a) * x[0]
        y, state.zi[key] = lfilter(b, a, x, zi=state.zi[key])
    else:
        y = lfilter(b, a, x)
    return y.astype(np.float32)


def _resample(audio, from_rate, to_rate):
    from scipy.signal import resample_poly
    from_rate, to_rate = int(from_rate), int(to_rate)
    g = gcd(from_rate, to_rate)
    return resample_poly(audio, to_rate // g, from_rate // g).astype(np.float32)


def _downconvert(iq, offset_hz, srate, state=None):
    """Shift IQ by offset_hz.  Uses state to keep mixing-tone phase continuous."""
    if abs(offset_hz) < 1.0:
        return iq
    n = len(iq)
    phase0 = state.downconv_phase if state is not None else 0.0
    idx = phase0 + np.arange(n, dtype=np.float64)
    phasor = np.exp((-2j * np.pi * offset_hz / srate) * idx).astype(np.complex64)
    if state is not None:
        # wrap at each full cycle to prevent float64 precision drift
        period = srate / abs(offset_hz)
        state.downconv_phase = (phase0 + n) % period
    return (iq * phasor).astype(np.complex64)


def demod_fm(iq, srate, channel_bw, audio_bw, deemph_tau, state=None):
    target = max(channel_bw * 2, AUDIO_RATE * 2)
    decim  = max(1, int(srate / target))
    if decim > 1:
        iq = _apply_lpf(iq, channel_bw / 2, srate, state=state, key="fm_ch")
        iq = iq[::decim]
    bb_rate = srate / decim

    # FM discriminator — prepend last sample so inter-block boundary is computed
    if state is not None and state.last_iq is not None:
        iq_ext = np.concatenate([[state.last_iq], iq])
    else:
        iq_ext = np.concatenate([[iq[0]], iq])  # first block: duplicate first sample → zero diff
    if state is not None:
        state.last_iq = iq[-1]
    fm = np.angle(iq_ext[1:] * np.conj(iq_ext[:-1])).astype(np.float32)

    # De-emphasis IIR (bypass when tau=0)
    if deemph_tau > 0:
        a = float(np.exp(-1.0 / (bb_rate * deemph_tau)))
        fm = _apply_iir([1.0 - a], [1.0, -a], fm, state=state, key="fm_deemph")

    # Audio lowpass
    fm = _apply_iir(_lpf(audio_bw, bb_rate, 63), [1.0], fm, state=state, key="fm_audio")

    audio = _resample(fm, bb_rate, AUDIO_RATE)
    peak = np.max(np.abs(audio))
    return audio / peak if peak > 1e-6 else audio


def demod_am(iq, srate, channel_bw, audio_bw, state=None):
    decim = max(1, int(srate / max(channel_bw * 4, AUDIO_RATE * 2)))
    if decim > 1:
        iq = _apply_lpf(iq, channel_bw / 2, srate, state=state, key="am_ch")
        iq = iq[::decim]
    bb_rate = srate / decim

    env = np.abs(iq).astype(np.float32)
    # DC block via single-pole high-pass (continuous across blocks)
    env = _apply_iir([1.0, -1.0], [1.0, -0.9995], env, state=state, key="am_dc")
    env = _apply_iir(_lpf(audio_bw, bb_rate, 63), [1.0], env, state=state, key="am_audio")
    audio = _resample(env, bb_rate, AUDIO_RATE)
    peak = np.max(np.abs(audio))
    return audio / peak if peak > 1e-6 else audio


def demod_ssb(iq, srate, audio_bw, mode, state=None):
    if mode == "lsb":
        iq = np.conj(iq)
    iq = _apply_lpf(iq, audio_bw, srate, ntaps=63, state=state, key="ssb_ch")
    decim = max(1, int(srate / (AUDIO_RATE * 2)))
    if decim > 1:
        iq = iq[::decim]
    bb_rate = srate / decim

    audio = np.real(iq).astype(np.float32)
    audio = _apply_iir([1.0, -1.0], [1.0, -0.995], audio, state=state, key="ssb_dc")
    audio = _resample(audio, bb_rate, AUDIO_RATE)
    peak = np.max(np.abs(audio))
    return audio / peak if peak > 1e-6 else audio


def demod_cw(iq, srate, bfo=700, state=None):
    n = len(iq)
    phase0 = state.bfo_phase if state is not None else 0.0
    idx = phase0 + np.arange(n, dtype=np.float64)
    bfo_phasor = np.exp((2j * np.pi * bfo / srate) * idx).astype(np.complex64)
    if state is not None:
        state.bfo_phase = (phase0 + n) % (srate / bfo)
    iq = (iq * bfo_phasor).astype(np.complex64)
    iq = _apply_lpf(iq, 500, srate, ntaps=63, state=state, key="cw_ch")
    decim = max(1, int(srate / (AUDIO_RATE * 2)))
    if decim > 1:
        iq = iq[::decim]
    bb_rate = srate / decim
    audio = np.real(iq).astype(np.float32)
    audio = _apply_iir([1.0, -1.0], [1.0, -0.995], audio, state=state, key="cw_dc")
    audio = _resample(audio, bb_rate, AUDIO_RATE)
    peak = np.max(np.abs(audio))
    return audio / peak if peak > 1e-6 else audio


def run_demod(iq, srate, mode, state=None):
    """Dispatch to mode-specific demod; returns float32 audio at AUDIO_RATE."""
    ch_bw, au_bw, deemph, _ = DEMOD_MODES[mode]
    if mode in ("WFM", "NFM"):
        return demod_fm(iq, srate, ch_bw, au_bw, deemph, state=state)
    if mode == "AM":
        return demod_am(iq, srate, ch_bw, au_bw, state=state)
    if mode in ("USB", "LSB"):
        return demod_ssb(iq, srate, au_bw, mode.lower(), state=state)
    if mode == "CW":
        return demod_cw(iq, srate, state=state)
    return np.zeros(0, dtype=np.float32)


# ──────────────────────────────────────────────────────────────────────────────
# Radio — SoapySDR wrapper
# ──────────────────────────────────────────────────────────────────────────────
class Radio:
    def __init__(self, driver, freq, srate, gain):
        results = SoapySDR.Device.enumerate(f"driver={driver}")
        if len(results) == 0:
            raise RuntimeError(f"No SoapySDR device found (driver={driver!r})")
        try:
            self.label = results[0]["label"]
        except Exception:
            self.label = driver
        self.sdr = SoapySDR.Device(f"driver={driver}")

        self.sdr.setSampleRate(SOAPY_SDR_RX, 0, srate)
        self.srate = self.sdr.getSampleRate(SOAPY_SDR_RX, 0)

        self.sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
        self.freq = self.sdr.getFrequency(SOAPY_SDR_RX, 0)

        try:
            self.sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        self._apply_gain(gain)

        self.stream = self.sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.sdr.activateStream(self.stream)

    def _apply_gain(self, g):
        try:
            self.sdr.setGain(SOAPY_SDR_RX, 0, float(np.clip(g, 0, 60)))
            self.gain = self.sdr.getGain(SOAPY_SDR_RX, 0)
        except Exception:
            self.gain = float(g)

    def set_freq(self, hz):
        self.sdr.setFrequency(SOAPY_SDR_RX, 0, hz)
        self.freq = self.sdr.getFrequency(SOAPY_SDR_RX, 0)

    def set_gain(self, db):
        self._apply_gain(db)

    def read(self, n):
        chunk_buf = np.empty(MAX_READ_CHUNK, dtype=np.complex64)
        result    = np.empty(n, dtype=np.complex64)
        got = 0
        while got < n:
            want = min(n - got, MAX_READ_CHUNK)
            sr   = self.sdr.readStream(self.stream, [chunk_buf], want,
                                       timeoutUs=2_000_000)
            if sr.ret < 0:
                raise RuntimeError(SoapySDR.errToStr(sr.ret))
            result[got:got + sr.ret] = chunk_buf[:sr.ret]
            got += sr.ret
        return result

    def deactivate(self):
        try:
            self.sdr.deactivateStream(self.stream)
        except Exception:
            pass

    def close(self):
        self.sdr.closeStream(self.stream)


# ──────────────────────────────────────────────────────────────────────────────
# Worker thread — QThread so libusb can register Qt socket notifiers
# ──────────────────────────────────────────────────────────────────────────────
class _WorkerThread(QThread):
    def __init__(self, app):
        super().__init__()
        self._app = app

    def run(self):
        self._app._worker_body()


# ──────────────────────────────────────────────────────────────────────────────
# Demodulator thread
# ──────────────────────────────────────────────────────────────────────────────
class _DemodThread(QThread):
    sig_level = pyqtSignal(float)   # signal power in dBFS

    def __init__(self, app):
        super().__init__()
        self._app = app

    def run(self):
        self._app._demod_body(self)


# ──────────────────────────────────────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────────────────────────────────────
class SpectrumWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SDR Spectrum Analyzer + Demodulator")
        self.resize(1380, 820)

        # Radio / worker
        self._radio      = None
        self._thread     = None
        self._stop_flag  = False
        self._calib_flag = False
        self._q          = queue.Queue(maxsize=2)

        # DSP state
        self._peak      = None
        self._win_cache = {}
        self._n         = 2048
        self._avg       = 4

        # Waterfall
        self._wf_buf = None
        self._wf_img = None

        # I/Q correction
        self._iq_enable    = True
        self._iq_dc_remove = True
        self._iq_alpha     = 1.0
        self._iq_phi       = 0.0

        # Crosshair / demodulator frequency
        self._demod_freq    = None   # Hz, set by left-click on plot
        self._demod_span1   = None   # axvspan for BW indicator (spectrum ax)
        self._demod_span2   = None   # axvspan for BW indicator (waterfall ax)

        # Demodulator
        self._demod_thread  = None
        self._demod_stop    = False
        self._demod_q       = queue.Queue(maxsize=64)
        self._demod_mode    = "WFM"
        self._demod_vol     = 0.8
        self._demod_squelch = -80.0

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    # ─────────────────────────────────────────────── UI construction ─────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_plot())
        splitter.setSizes([270, 1110])
        splitter.setHandleWidth(3)
        layout.addWidget(splitter)

        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready — configure and press Start.")

    # ──────────────────────────────────────────────────── Sidebar ────────

    def _build_sidebar(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(220)
        scroll.setMaximumWidth(480)

        container = QWidget()
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(8)

        # ── Frequency ────────────────────────────────────────────────────
        grp = QGroupBox("Center Frequency")
        g = QVBoxLayout(grp)
        row = QHBoxLayout()
        self._freq_edit = QLineEdit("100.0000")
        self._freq_edit.returnPressed.connect(self._on_freq_apply)
        row.addWidget(self._freq_edit)
        row.addWidget(QLabel("MHz"))
        ab = QPushButton("Apply")
        ab.clicked.connect(self._on_freq_apply)
        row.addWidget(ab)
        g.addLayout(row)
        steps = QHBoxLayout()
        for lbl, d in [("−1M", -1e6), ("−100k", -100e3), ("+100k", 100e3), ("+1M", 1e6)]:
            b = QPushButton(lbl)
            b.setFixedHeight(24)
            b.clicked.connect(lambda _, dd=d: self._on_tune_step(dd))
            steps.addWidget(b)
        g.addLayout(steps)
        vlay.addWidget(grp)

        # ── Sample rate ──────────────────────────────────────────────────
        grp = QGroupBox("Sample Rate")
        g = QVBoxLayout(grp)
        self._srate_combo = QComboBox()
        self._srate_combo.addItems(SRATE_LABELS)
        self._srate_combo.setToolTip("Restart required to change")
        g.addWidget(self._srate_combo)
        vlay.addWidget(grp)

        # ── Gain ─────────────────────────────────────────────────────────
        grp = QGroupBox("Gain (dB)")
        g = QVBoxLayout(grp)
        row = QHBoxLayout()
        self._gain_slider = QSlider(Qt.Horizontal)
        self._gain_slider.setRange(0, 60)
        self._gain_slider.setValue(30)
        self._gain_label = QLabel("30 dB")
        self._gain_label.setFixedWidth(42)
        self._gain_slider.valueChanged.connect(self._on_gain_changed)
        row.addWidget(self._gain_slider)
        row.addWidget(self._gain_label)
        g.addLayout(row)
        vlay.addWidget(grp)

        # ── DSP ──────────────────────────────────────────────────────────
        grp = QGroupBox("DSP")
        g = QGridLayout(grp)
        g.setSpacing(6)
        g.addWidget(QLabel("FFT size"), 0, 0)
        self._fft_combo = QComboBox()
        self._fft_combo.addItems(["512", "1024", "2048", "4096", "8192"])
        self._fft_combo.setCurrentIndex(2)
        self._fft_combo.setToolTip("Restart required to change")
        g.addWidget(self._fft_combo, 0, 1)
        g.addWidget(QLabel("Averaging"), 1, 0)
        self._avg_spin = QSpinBox()
        self._avg_spin.setRange(1, 32)
        self._avg_spin.setValue(4)
        self._avg_spin.valueChanged.connect(lambda v: setattr(self, '_avg', v))
        g.addWidget(self._avg_spin, 1, 1)
        vlay.addWidget(grp)

        # ── Y Range ──────────────────────────────────────────────────────
        grp = QGroupBox("Y Range (dBFS)")
        g = QGridLayout(grp)
        g.setSpacing(6)
        g.addWidget(QLabel("Min"), 0, 0)
        self._ymin_spin = QDoubleSpinBox()
        self._ymin_spin.setRange(-160, -10)
        self._ymin_spin.setSingleStep(5)
        self._ymin_spin.setValue(-100)
        self._ymin_spin.valueChanged.connect(self._on_yrange_changed)
        g.addWidget(self._ymin_spin, 0, 1)
        g.addWidget(QLabel("Max"), 1, 0)
        self._ymax_spin = QDoubleSpinBox()
        self._ymax_spin.setRange(-80, 20)
        self._ymax_spin.setSingleStep(5)
        self._ymax_spin.setValue(0)
        self._ymax_spin.valueChanged.connect(self._on_yrange_changed)
        g.addWidget(self._ymax_spin, 1, 1)
        self._autoscale_y_chk = QCheckBox("Auto Y  (peak + 10 dB)")
        self._autoscale_y_chk.setChecked(True)
        self._autoscale_y_chk.stateChanged.connect(
            lambda s: (self._ymin_spin.setEnabled(not bool(s)),
                       self._ymax_spin.setEnabled(not bool(s))))
        g.addWidget(self._autoscale_y_chk, 2, 0, 1, 2)
        vlay.addWidget(grp)

        # ── Waterfall ────────────────────────────────────────────────────
        grp = QGroupBox("Waterfall")
        g = QGridLayout(grp)
        g.setSpacing(6)
        g.addWidget(QLabel("Colormap"), 0, 0)
        self._cmap_combo = QComboBox()
        self._cmap_combo.addItems(WF_CMAPS)
        self._cmap_combo.currentTextChanged.connect(self._on_cmap_changed)
        g.addWidget(self._cmap_combo, 0, 1)
        g.addWidget(QLabel("Rows"), 1, 0)
        self._wf_rows_spin = QSpinBox()
        self._wf_rows_spin.setRange(50, 500)
        self._wf_rows_spin.setSingleStep(50)
        self._wf_rows_spin.setValue(WATERFALL_ROWS)
        self._wf_rows_spin.setToolTip("History depth — restart to apply")
        g.addWidget(self._wf_rows_spin, 1, 1)
        vlay.addWidget(grp)

        # ── I/Q Correction ───────────────────────────────────────────────
        grp = QGroupBox("I/Q Correction")
        g = QGridLayout(grp)
        g.setSpacing(5)
        self._iq_enable_chk = QCheckBox("Enable correction")
        self._iq_enable_chk.setChecked(True)
        self._iq_enable_chk.stateChanged.connect(self._on_iq_enable_changed)
        g.addWidget(self._iq_enable_chk, 0, 0, 1, 2)
        self._iq_dc_chk = QCheckBox("Remove DC offset")
        self._iq_dc_chk.setChecked(True)
        self._iq_dc_chk.stateChanged.connect(
            lambda v: setattr(self, '_iq_dc_remove', bool(v)))
        g.addWidget(self._iq_dc_chk, 1, 0, 1, 2)
        g.addWidget(QLabel("Ampl α"), 2, 0)
        self._iq_alpha_spin = QDoubleSpinBox()
        self._iq_alpha_spin.setRange(0.5, 2.0)
        self._iq_alpha_spin.setSingleStep(0.001)
        self._iq_alpha_spin.setDecimals(4)
        self._iq_alpha_spin.setValue(1.0)
        self._iq_alpha_spin.valueChanged.connect(self._on_iq_alpha_changed)
        g.addWidget(self._iq_alpha_spin, 2, 1)
        g.addWidget(QLabel("Phase φ"), 3, 0)
        self._iq_phi_spin = QDoubleSpinBox()
        self._iq_phi_spin.setRange(-20.0, 20.0)
        self._iq_phi_spin.setSingleStep(0.01)
        self._iq_phi_spin.setDecimals(3)
        self._iq_phi_spin.setValue(0.0)
        self._iq_phi_spin.setSuffix(" °")
        self._iq_phi_spin.valueChanged.connect(self._on_iq_phi_changed)
        g.addWidget(self._iq_phi_spin, 3, 1)
        self._irr_label = QLabel("IRR: —")
        self._irr_label.setStyleSheet("color: #88aaff; font-size: 11px;")
        g.addWidget(self._irr_label, 4, 0, 1, 2)
        btn_row = QHBoxLayout()
        cb = QPushButton("⟳ Auto Calibrate")
        cb.clicked.connect(self._on_auto_calibrate)
        btn_row.addWidget(cb)
        rb = QPushButton("Reset")
        rb.clicked.connect(self._on_reset_iq)
        btn_row.addWidget(rb)
        g.addLayout(btn_row, 5, 0, 1, 2)
        vlay.addWidget(grp)

        # ── Demodulator ──────────────────────────────────────────────────
        grp = QGroupBox("Demodulator")
        g = QGridLayout(grp)
        g.setSpacing(5)

        # Demod frequency display (set by clicking on plot)
        g.addWidget(QLabel("Demod freq"), 0, 0)
        self._demod_freq_lbl = QLabel("—")
        self._demod_freq_lbl.setStyleSheet(
            "color:#00ffff; font-weight:bold; font-size:12px;")
        g.addWidget(self._demod_freq_lbl, 0, 1)

        # Cursor frequency (hover)
        g.addWidget(QLabel("Cursor"), 1, 0)
        self._cursor_lbl = QLabel("—")
        self._cursor_lbl.setStyleSheet("color:#ffee00; font-size:11px;")
        g.addWidget(self._cursor_lbl, 1, 1)

        hint = QLabel("← Left-click plot to set")
        hint.setStyleSheet("color:#555577; font-size:10px;")
        g.addWidget(hint, 2, 0, 1, 2)

        # Mode
        g.addWidget(QLabel("Mode"), 3, 0)
        self._demod_mode_combo = QComboBox()
        for k, v in DEMOD_MODES.items():
            self._demod_mode_combo.addItem(f"{k} — {v[3]}", k)
        self._demod_mode_combo.currentIndexChanged.connect(self._on_demod_mode_changed)
        g.addWidget(self._demod_mode_combo, 3, 1)

        # BW readout
        g.addWidget(QLabel("Bandwidth"), 4, 0)
        self._demod_bw_lbl = QLabel("200 kHz")
        self._demod_bw_lbl.setStyleSheet("color:#aaaacc;")
        g.addWidget(self._demod_bw_lbl, 4, 1)

        # Volume
        g.addWidget(QLabel("Volume"), 5, 0)
        vol_row = QHBoxLayout()
        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_lbl = QLabel("80")
        self._vol_lbl.setFixedWidth(28)
        self._vol_slider.valueChanged.connect(
            lambda v: (setattr(self, '_demod_vol', v / 100.0),
                       self._vol_lbl.setText(str(v))))
        vol_row.addWidget(self._vol_slider)
        vol_row.addWidget(self._vol_lbl)
        vol_w = QWidget()
        vol_w.setLayout(vol_row)
        g.addWidget(vol_w, 5, 1)

        # Squelch
        g.addWidget(QLabel("Squelch"), 6, 0)
        sq_row = QHBoxLayout()
        self._sq_slider = QSlider(Qt.Horizontal)
        self._sq_slider.setRange(-120, 0)
        self._sq_slider.setValue(-80)
        self._sq_lbl = QLabel("-80")
        self._sq_lbl.setFixedWidth(32)
        self._sq_slider.valueChanged.connect(
            lambda v: (setattr(self, '_demod_squelch', float(v)),
                       self._sq_lbl.setText(str(v))))
        sq_row.addWidget(self._sq_slider)
        sq_row.addWidget(self._sq_lbl)
        sq_w = QWidget()
        sq_w.setLayout(sq_row)
        g.addWidget(sq_w, 6, 1)

        # Signal level bar
        g.addWidget(QLabel("Signal"), 7, 0)
        self._sig_bar = QProgressBar()
        self._sig_bar.setRange(-100, 0)
        self._sig_bar.setValue(-100)
        self._sig_bar.setFormat("%v dBFS")
        self._sig_bar.setFixedHeight(16)
        g.addWidget(self._sig_bar, 7, 1)

        # Demod start/stop + clear
        btn_row2 = QHBoxLayout()
        self._demod_btn = QPushButton("▶ Demod")
        self._demod_btn.setObjectName("demodBtn")
        self._demod_btn.setProperty("active", "false")
        self._demod_btn.clicked.connect(self._on_demod_startstop)
        if not AUDIO_OK:
            self._demod_btn.setEnabled(False)
            self._demod_btn.setToolTip(
                "sounddevice / PortAudio not available\n"
                "sudo apt install libportaudio2")
        btn_row2.addWidget(self._demod_btn)

        clear_btn = QPushButton("✕ Clear")
        clear_btn.setToolTip("Remove demod frequency marker")
        clear_btn.clicked.connect(self._on_demod_clear)
        btn_row2.addWidget(clear_btn)
        g.addLayout(btn_row2, 8, 0, 1, 2)

        vlay.addWidget(grp)

        # ── Control ──────────────────────────────────────────────────────
        grp = QGroupBox("Control")
        g = QVBoxLayout(grp)
        self._start_btn = QPushButton("▶  Start")
        self._start_btn.setObjectName("startBtn")
        self._start_btn.setProperty("running", "false")
        self._start_btn.setFixedHeight(36)
        self._start_btn.clicked.connect(self._on_start_stop)
        g.addWidget(self._start_btn)
        row2 = QHBoxLayout()
        rp = QPushButton("↑ Reset Peak")
        rp.clicked.connect(lambda: self._reset_peak())
        row2.addWidget(rp)
        fx = QPushButton("⟷ Fit X")
        fx.clicked.connect(self._on_fit_x)
        row2.addWidget(fx)
        g.addLayout(row2)
        vlay.addWidget(grp)

        vlay.addStretch()
        scroll.setWidget(container)
        return scroll

    # ──────────────────────────────────────────────────── Plot ───────────

    def _build_plot(self):
        n     = int(self._fft_combo.currentText())
        empty = np.full(n, -100.0)
        freqs = np.linspace(99.0, 101.0, n)

        # ── Spectrum figure ───────────────────────────────────────────────
        self._fig = Figure(facecolor=DARK)
        self._fig.subplots_adjust(left=0.07, right=0.97, top=0.88, bottom=0.10)
        self._ax = self._fig.add_subplot(111, facecolor=PLOTBG)

        self._line,      = self._ax.plot(freqs, empty, color=ACCENT,
                                         linewidth=0.7, label="Live")
        self._peak_line, = self._ax.plot(freqs, empty, color=PEAK_C,
                                         linewidth=0.5, alpha=0.65,
                                         linestyle="--", label="Peak hold")

        self._ax.set_ylim(-100, 0)
        self._ax.set_xlim(freqs[0], freqs[-1])
        self._ax.set_ylabel("Power (dBFS)", color="#999999")
        self._ax.set_xlabel("Frequency (MHz)", color="#999999")
        self._ax.tick_params(colors="#999999")
        self._ax.xaxis.set_major_formatter(EngFormatter(unit=""))
        for sp in self._ax.spines.values():
            sp.set_edgecolor("#2a2a44")
        self._ax.grid(True, color="#1a1a33", linewidth=0.5)
        self._ax.legend(facecolor=DARK, labelcolor="#aaaaaa",
                        loc="upper right", fontsize=8)
        self._plot_title = self._ax.set_title(
            "Configure settings and press  ▶ Start",
            color="#cccccc", fontsize=10, pad=6)

        self._xh_v1 = self._ax.axvline(x=100, color="#ffee00", lw=0.9,
                                        ls="--", alpha=0.85, visible=False, zorder=6)
        self._xh_txt = self._ax.text(
            0.5, 0.97, "", transform=self._ax.transAxes,
            ha="center", va="top", color="#ffee00", fontsize=9,
            fontfamily="monospace", zorder=7,
            bbox=dict(boxstyle="round,pad=0.2", fc="#111122", alpha=0.7, ec="none"))
        self._dm_v1 = self._ax.axvline(x=100, color="#00ffff", lw=1.5,
                                        visible=False, zorder=5)
        self._demod_span1 = None

        self._canvas = FigureCanvas(self._fig)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas.mpl_connect("motion_notify_event",  self._on_mouse_move)
        self._canvas.mpl_connect("button_press_event",   self._on_mouse_click)
        self._canvas.mpl_connect("axes_leave_event",     self._on_axes_leave)

        # ── Waterfall figure ──────────────────────────────────────────────
        self._fig_wf = Figure(facecolor=DARK)
        self._fig_wf.subplots_adjust(left=0.07, right=0.97, top=0.97, bottom=0.14)
        self._wf_ax = self._fig_wf.add_subplot(111, facecolor=PLOTBG)

        wf_empty = np.full((WATERFALL_ROWS, n), -100.0)
        self._wf_img = self._wf_ax.imshow(
            wf_empty, aspect="auto", origin="upper",
            extent=[freqs[0], freqs[-1], WATERFALL_ROWS, 0],
            cmap=WF_CMAPS[0], vmin=-100, vmax=0, interpolation="nearest")
        self._wf_ax.set_xlabel("Frequency (MHz)", color="#999999")
        self._wf_ax.set_ylabel("Time →", color="#666688", fontsize=8)
        self._wf_ax.tick_params(colors="#999999")
        self._wf_ax.xaxis.set_major_formatter(EngFormatter(unit=""))
        for sp in self._wf_ax.spines.values():
            sp.set_edgecolor("#2a2a44")
        self._wf_ax.set_yticks([])

        self._xh_v2 = self._wf_ax.axvline(x=100, color="#ffee00", lw=0.9,
                                            ls="--", alpha=0.85, visible=False, zorder=6)
        self._dm_v2 = self._wf_ax.axvline(x=100, color="#00ffff", lw=1.5,
                                            visible=False, zorder=5)
        self._demod_span2 = None

        self._canvas_wf = FigureCanvas(self._fig_wf)
        self._canvas_wf.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas_wf.mpl_connect("motion_notify_event",  self._on_mouse_move)
        self._canvas_wf.mpl_connect("button_press_event",   self._on_mouse_click)
        self._canvas_wf.mpl_connect("axes_leave_event",     self._on_axes_leave)

        # ── X-axis sync (replaces sharex across two figures) ─────────────
        # Only propagate limits — do NOT call draw_idle here; the existing
        # mouse/tick handlers already schedule redraws and draw_idle inside
        # an xlim_changed callback triggers another xlim_changed → recursion.
        self._xlim_syncing = False

        def _spec_xlim_changed(ax):
            if not self._xlim_syncing:
                self._xlim_syncing = True
                self._wf_ax.set_xlim(ax.get_xlim())
                self._xlim_syncing = False

        def _wf_xlim_changed(ax):
            if not self._xlim_syncing:
                self._xlim_syncing = True
                self._ax.set_xlim(ax.get_xlim())
                self._xlim_syncing = False

        self._ax.callbacks.connect("xlim_changed", _spec_xlim_changed)
        self._wf_ax.callbacks.connect("xlim_changed", _wf_xlim_changed)

        # ── Vertical splitter ─────────────────────────────────────────────
        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self._canvas)
        vsplit.addWidget(self._canvas_wf)
        vsplit.setSizes([600, 250])
        vsplit.setHandleWidth(4)
        return vsplit

    # ──────────────────────────────────────────────────── Helpers ────────

    def _get_window(self, n):
        if n not in self._win_cache:
            w = np.blackman(n).astype(np.float32)
            self._win_cache[n] = (w, float(np.mean(w ** 2)))
        return self._win_cache[n]

    def _psd_db(self, samples, n):
        win, wp = self._get_window(n)
        spec = np.fft.fftshift(np.fft.fft(samples[:n] * win, n))
        return 10.0 * np.log10((np.abs(spec) ** 2) / (n * wp) + 1e-30)

    def _freq_axis(self, center_hz, srate, n):
        return (np.fft.fftshift(np.fft.fftfreq(n, 1.0 / srate)) + center_hz) * 1e-6

    def _reset_peak(self):
        if self._peak is not None:
            self._peak[:] = self._ymin_spin.value()

    def _draw_idle(self):
        self._canvas.draw_idle()
        self._canvas_wf.draw_idle()

    def _retune_axes(self, center_hz, srate, n):
        freqs = self._freq_axis(center_hz, srate, n)
        self._line.set_xdata(freqs)
        self._peak_line.set_xdata(freqs)
        self._ax.set_xlim(freqs[0], freqs[-1])
        self._wf_ax.set_xlim(freqs[0], freqs[-1])
        self._wf_img.set_extent([freqs[0], freqs[-1], WATERFALL_ROWS, 0])
        self._reset_peak()

    def _update_y_range(self, ymin, ymax):
        self._ax.set_ylim(ymin, ymax)
        self._wf_img.set_clim(ymin, ymax)
        self._ymin_spin.blockSignals(True)
        self._ymax_spin.blockSignals(True)
        self._ymin_spin.setValue(ymin)
        self._ymax_spin.setValue(ymax)
        self._ymin_spin.blockSignals(False)
        self._ymax_spin.blockSignals(False)

    def _refresh_irr(self):
        irr = _irr_db(self._iq_alpha, self._iq_phi)
        self._irr_label.setText(
            "IRR: ∞ dB (ideal)" if irr == float("inf") else f"IRR: {irr:.1f} dB")

    def _update_demod_marker(self):
        """Redraw cyan demod frequency line + BW shading on both axes."""
        if self._demod_freq is None:
            return
        f_mhz = self._demod_freq / 1e6
        mode   = self._demod_mode
        ch_bw, *_ = DEMOD_MODES[mode]
        bw_mhz = ch_bw / 1e6

        # Center lines
        for v in (self._dm_v1, self._dm_v2):
            v.set_xdata([f_mhz, f_mhz])
            v.set_visible(True)

        # BW shading — remove old spans and recreate
        for attr in ("_demod_span1", "_demod_span2"):
            sp = getattr(self, attr)
            if sp is not None:
                try:
                    sp.remove()
                except Exception:
                    pass
        if mode == "USB":
            x1, x2 = f_mhz, f_mhz + bw_mhz
        elif mode == "LSB":
            x1, x2 = f_mhz - bw_mhz, f_mhz
        else:
            x1, x2 = f_mhz - bw_mhz / 2, f_mhz + bw_mhz / 2

        self._demod_span1 = self._ax.axvspan(
            x1, x2, alpha=0.10, color="#00ffff", zorder=3)
        self._demod_span2 = self._wf_ax.axvspan(
            x1, x2, alpha=0.18, color="#00ffff", zorder=3)

    # ─────────────────────────────────────────────── Mouse handlers ───────

    def _on_mouse_move(self, event):
        if event.inaxes not in (self._ax, self._wf_ax) or event.xdata is None:
            return
        x = event.xdata  # MHz
        self._xh_v1.set_xdata([x, x])
        self._xh_v2.set_xdata([x, x])
        self._xh_v1.set_visible(True)
        self._xh_v2.set_visible(True)
        self._xh_txt.set_text(f"{x:.4f} MHz")
        self._cursor_lbl.setText(f"{x:.4f} MHz")
        self._draw_idle()

    def _on_axes_leave(self, event):
        self._xh_v1.set_visible(False)
        self._xh_v2.set_visible(False)
        self._xh_txt.set_text("")
        self._cursor_lbl.setText("—")
        self._draw_idle()

    def _on_mouse_click(self, event):
        if event.inaxes not in (self._ax, self._wf_ax) or event.xdata is None:
            return
        if event.button == 1:   # left click → set demod frequency
            self._demod_freq = event.xdata * 1e6
            self._demod_freq_lbl.setText(f"{event.xdata:.4f} MHz")
            self._update_demod_marker()
            self._draw_idle()

    # ─────────────────────────────────────────────────────── Handlers ────

    def _on_freq_apply(self):
        try:
            mhz = float(self._freq_edit.text())
        except ValueError:
            return
        if self._radio is not None:
            self._radio.set_freq(mhz * 1e6)
            self._freq_edit.setText(f"{self._radio.freq/1e6:.4f}")
            self._retune_axes(self._radio.freq, self._radio.srate, self._n)
            if self._wf_buf is not None:
                self._wf_buf[:] = self._ymin_spin.value()
            self._draw_idle()

    def _on_tune_step(self, delta):
        try:
            cur = float(self._freq_edit.text())
        except ValueError:
            cur = 100.0
        self._freq_edit.setText(f"{cur + delta/1e6:.4f}")
        self._on_freq_apply()

    def _on_gain_changed(self, value):
        self._gain_label.setText(f"{value} dB")
        if self._radio is not None:
            self._radio.set_gain(value)

    def _on_yrange_changed(self):
        ymin, ymax = self._ymin_spin.value(), self._ymax_spin.value()
        if ymin < ymax:
            self._ax.set_ylim(ymin, ymax)
            self._wf_img.set_clim(ymin, ymax)
            self._draw_idle()

    def _on_cmap_changed(self, name):
        self._wf_img.set_cmap(name)
        self._draw_idle()

    def _on_fit_x(self):
        if self._radio is not None:
            freqs = self._freq_axis(self._radio.freq, self._radio.srate, self._n)
            self._ax.set_xlim(freqs[0], freqs[-1])
            self._draw_idle()

    def _on_iq_enable_changed(self, state):
        self._iq_enable = bool(state)
        self._iq_alpha_spin.setEnabled(bool(state))
        self._iq_phi_spin.setEnabled(bool(state))

    def _on_iq_alpha_changed(self, v):
        self._iq_alpha = v
        self._refresh_irr()

    def _on_iq_phi_changed(self, v):
        self._iq_phi = float(np.radians(v))
        self._refresh_irr()

    def _on_auto_calibrate(self):
        if self._radio is None:
            self._statusbar.showMessage("Start the radio first.")
            return
        self._statusbar.showMessage("Auto-calibrating I/Q imbalance…")
        self._calib_flag = True

    def _on_reset_iq(self):
        self._iq_alpha = 1.0
        self._iq_phi   = 0.0
        for s, v in ((self._iq_alpha_spin, 1.0), (self._iq_phi_spin, 0.0)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        self._refresh_irr()

    def _on_demod_mode_changed(self, _):
        self._demod_mode = self._demod_mode_combo.currentData()
        ch_bw = DEMOD_MODES[self._demod_mode][0]
        self._demod_bw_lbl.setText(
            f"{ch_bw/1000:.1f} kHz" if ch_bw < 1_000_000
            else f"{ch_bw/1_000_000:.3f} MHz")
        if self._demod_freq is not None:
            self._update_demod_marker()
            self._draw_idle()

    def _on_demod_startstop(self):
        if self._demod_thread is None:
            self._do_start_demod()
        else:
            self._do_stop_demod()

    def _on_demod_clear(self):
        self._demod_freq = None
        self._demod_freq_lbl.setText("—")
        for v in (self._dm_v1, self._dm_v2):
            v.set_visible(False)
        for attr in ("_demod_span1", "_demod_span2"):
            sp = getattr(self, attr)
            if sp is not None:
                try:
                    sp.remove()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._draw_idle()

    def _on_start_stop(self):
        if self._radio is None:
            self._do_start()
        else:
            self._do_stop()

    # ───────────────────────────────────────────── Demod start / stop ────

    def _do_start_demod(self):
        if self._radio is None:
            self._statusbar.showMessage("Start the radio first.")
            return
        if self._demod_freq is None:
            self._statusbar.showMessage(
                "Click on the spectrum to set a demodulator frequency first.")
            return
        self._demod_stop   = False
        self._demod_thread = _DemodThread(self)
        self._demod_thread.sig_level.connect(self._on_demod_level)
        self._demod_thread.start()

        self._demod_btn.setText("■ Stop Demod")
        self._demod_btn.setProperty("active", "true")
        self._demod_btn.style().unpolish(self._demod_btn)
        self._demod_btn.style().polish(self._demod_btn)
        self._statusbar.showMessage(
            f"Demodulating {self._demod_mode}  @  "
            f"{self._demod_freq/1e6:.4f} MHz")

    def _do_stop_demod(self):
        self._demod_stop = True
        if self._demod_thread is not None:
            self._demod_thread.wait(3000)
            self._demod_thread = None
        while not self._demod_q.empty():
            try:
                self._demod_q.get_nowait()
            except queue.Empty:
                break
        self._demod_btn.setText("▶ Demod")
        self._demod_btn.setProperty("active", "false")
        self._demod_btn.style().unpolish(self._demod_btn)
        self._demod_btn.style().polish(self._demod_btn)
        self._sig_bar.setValue(-100)

    def _on_demod_level(self, db):
        self._sig_bar.setValue(int(max(-100, min(0, db))))

    # ─────────────────────────────────────────── Radio start / stop ──────

    def _do_start(self):
        try:
            freq_hz = float(self._freq_edit.text()) * 1e6
        except ValueError:
            self._statusbar.showMessage("Invalid frequency.")
            return

        srate = VALID_SRATES[self._srate_combo.currentIndex()]
        gain  = self._gain_slider.value()
        self._n   = int(self._fft_combo.currentText())
        self._avg = self._avg_spin.value()

        # Radio is opened inside the worker QThread so that libusb can register
        # Qt QSocketNotifier objects from a proper QThread context.
        self._pending_freq  = freq_hz
        self._pending_srate = srate
        self._pending_gain  = gain

        ymin = self._ymin_spin.value()
        self._peak   = np.full(self._n, ymin, dtype=np.float32)
        self._wf_buf = np.full((self._wf_rows_spin.value(), self._n),
                               ymin, dtype=np.float32)
        self._wf_img.set_data(self._wf_buf)
        self._retune_axes(freq_hz, srate, self._n)
        self._line.set_ydata(np.full(self._n, ymin))
        self._peak_line.set_ydata(self._peak)
        self._refresh_irr()

        self._srate_combo.setEnabled(False)
        self._fft_combo.setEnabled(False)
        self._start_btn.setText("■  Stop")
        self._start_btn.setProperty("running", "true")
        self._start_btn.style().unpolish(self._start_btn)
        self._start_btn.style().polish(self._start_btn)

        self._stop_flag  = False
        self._calib_flag = False
        self._thread = _WorkerThread(self)
        self._thread.start()
        self._timer.start()

        self._statusbar.showMessage("Opening device…")

    def _do_stop(self):
        if self._demod_thread is not None:
            self._do_stop_demod()
        self._timer.stop()
        self._stop_flag  = True
        self._calib_flag = False
        if self._radio is not None:
            self._radio.deactivate()
        if self._thread is not None:
            self._thread.wait(4000)
            self._thread = None
        if self._radio is not None:
            self._radio.close()
            self._radio = None
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._srate_combo.setEnabled(True)
        self._fft_combo.setEnabled(True)
        self._start_btn.setText("▶  Start")
        self._start_btn.setProperty("running", "false")
        self._start_btn.style().unpolish(self._start_btn)
        self._start_btn.style().polish(self._start_btn)
        self._plot_title.set_text("Stopped — press  ▶ Start to resume")
        self._draw_idle()
        self._statusbar.showMessage("Stopped.")

    def closeEvent(self, event):
        if self._radio is not None:
            self._do_stop()
        event.accept()

    # ──────────────────────────────── Worker body (runs in _WorkerThread) ─

    def _worker_body(self):
        # Open device here — inside QThread — so libusb registers
        # Qt QSocketNotifier objects from a proper QThread context.
        try:
            radio = Radio("miri",
                          self._pending_freq,
                          self._pending_srate,
                          self._pending_gain)
        except Exception as exc:
            try:
                self._q.put_nowait(("error", str(exc)))
            except queue.Full:
                pass
            return
        self._radio = radio

        while not self._stop_flag:
            try:
                if self._calib_flag:
                    self._calib_flag = False
                    n_cal = min(self._n * 8, 16_384)
                    cal   = radio.read(n_cal)
                    alpha, phi, irr = estimate_iq_imbalance(cal)
                    try:
                        self._q.put_nowait(("calibration", alpha, phi, irr))
                    except queue.Full:
                        pass
                    continue

                n, avg = self._n, self._avg
                acc = np.zeros(n, dtype=np.float64)
                for _ in range(avg):
                    if self._stop_flag:
                        return
                    raw = radio.read(n)
                    if self._iq_dc_remove or self._iq_enable:
                        raw = iq_correct(
                            raw,
                            self._iq_alpha if self._iq_enable else 1.0,
                            self._iq_phi   if self._iq_enable else 0.0,
                            self._iq_dc_remove)
                    # Feed corrected IQ to demodulator
                    if self._demod_thread is not None and not self._demod_stop:
                        try:
                            if self._demod_q.full():
                                self._demod_q.get_nowait()
                            self._demod_q.put_nowait(raw.copy())
                        except queue.Empty:
                            pass
                    acc += self._psd_db(raw, n)

                psd = (acc / avg).astype(np.float32)
                if self._q.full():
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                self._q.put_nowait((psd, radio.freq, radio.srate, radio.gain))

            except Exception as exc:
                if not self._stop_flag:
                    try:
                        self._q.put_nowait(("error", str(exc)))
                    except queue.Full:
                        pass
                return

    # ────────────────────────── Demod body (runs in _DemodThread) ─────────

    def _demod_body(self, thread: _DemodThread):
        import sounddevice as sd

        WRITE_CHUNK  = 2048   # audio samples per stream.write()
        ACCUM_SECS   = 0.025  # accumulate this many seconds of IQ before demodulating
        accum_size   = None   # set on first frame (depends on srate)

        # IQ accumulator — keep a list of segments, flatten only when ready
        iq_segs      : list  = []
        iq_segs_len  : int   = 0
        audio_buf    = np.empty(0, dtype=np.float32)

        state      = _DemodState()
        cur_mode   = self._demod_mode
        cur_offset = None
        last_sig_db = -100.0

        try:
            with sd.OutputStream(samplerate=AUDIO_RATE, channels=1,
                                  dtype="float32", blocksize=WRITE_CHUNK,
                                  latency="low") as stream:
                while not self._demod_stop:
                    try:
                        iq = self._demod_q.get(timeout=0.15)
                    except queue.Empty:
                        continue

                    radio = self._radio
                    if radio is None or self._demod_freq is None:
                        continue

                    if accum_size is None:
                        # ~25 ms worth of IQ samples, rounded to a power of two
                        accum_size = max(4096,
                            1 << int(np.log2(radio.srate * ACCUM_SECS)))

                    offset = self._demod_freq - radio.freq

                    # Reset on mode or frequency change (>1 kHz)
                    if self._demod_mode != cur_mode or (
                            cur_offset is not None and
                            abs(offset - cur_offset) > 1000):
                        state       = _DemodState()
                        audio_buf   = np.empty(0, dtype=np.float32)
                        iq_segs     = []
                        iq_segs_len = 0
                        cur_mode    = self._demod_mode
                    cur_offset = offset

                    # Signal level reported on every incoming block (stays responsive)
                    pwr        = float(np.mean(np.abs(iq) ** 2))
                    last_sig_db = 10.0 * np.log10(pwr + 1e-30)
                    thread.sig_level.emit(last_sig_db)

                    # Downconvert (phase-continuous) and accumulate
                    iq_segs.append(_downconvert(iq, offset, radio.srate, state=state))
                    iq_segs_len += len(iq)

                    # Wait until we have a full working block
                    if iq_segs_len < accum_size:
                        continue

                    iq_block  = np.concatenate(iq_segs)
                    iq_segs   = [iq_block[accum_size:]]   # keep leftover
                    iq_segs_len = len(iq_segs[0])
                    iq_block  = iq_block[:accum_size]

                    if last_sig_db < self._demod_squelch:
                        n_sil = int(accum_size * AUDIO_RATE / radio.srate)
                        audio_buf = np.concatenate(
                            [audio_buf, np.zeros(n_sil, dtype=np.float32)])
                    else:
                        try:
                            audio = run_demod(iq_block, radio.srate,
                                              self._demod_mode, state=state)
                            audio = np.clip(audio * self._demod_vol, -1.0, 1.0)
                            audio_buf = np.concatenate([audio_buf, audio])
                        except Exception:
                            continue

                    # Write completed chunks to sounddevice
                    while len(audio_buf) >= WRITE_CHUNK:
                        chunk = audio_buf[:WRITE_CHUNK].reshape(-1, 1)
                        audio_buf = audio_buf[WRITE_CHUNK:]
                        if not self._demod_stop:
                            stream.write(chunk)

        except Exception as exc:
            try:
                self._q.put_nowait(("error", f"Demod audio: {exc}"))
            except queue.Full:
                pass

    # ──────────────────────────────────────── Tick (Qt main thread) ───────

    def _tick(self):
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return

        tag = item[0] if isinstance(item[0], str) else None

        if tag == "error":
            self._statusbar.showMessage(f"Error: {item[1]}")
            self._do_stop()
            return

        if tag == "calibration":
            _, alpha, phi, irr = item
            for sp, v in ((self._iq_alpha_spin, alpha),
                          (self._iq_phi_spin, float(np.degrees(phi)))):
                sp.blockSignals(True)
                sp.setValue(v)
                sp.blockSignals(False)
            self._iq_alpha = alpha
            self._iq_phi   = phi
            irr_s = f"{irr:.1f} dB" if irr < 1e6 else "∞ dB"
            self._irr_label.setText(f"IRR: {irr_s}")
            self._statusbar.showMessage(
                f"Calibration done — α={alpha:.4f}  φ={np.degrees(phi):.3f}°  IRR={irr_s}")
            return

        psd, center_hz, srate, gain = item

        ymin = self._ymin_spin.value()
        if self._peak is None or len(self._peak) != len(psd):
            self._peak = np.full(len(psd), ymin, dtype=np.float32)
        np.maximum(self._peak, psd, out=self._peak)

        # Auto Y
        if self._autoscale_y_chk.isChecked():
            pk       = float(np.max(psd))
            new_ymax = round(pk + 10, 0)
            cur_span = self._ymax_spin.value() - self._ymin_spin.value()
            self._update_y_range(new_ymax - max(cur_span, 40), new_ymax)

        self._line.set_ydata(psd)
        self._peak_line.set_ydata(self._peak)

        # Waterfall
        if self._wf_buf is not None and self._wf_buf.shape[1] == len(psd):
            self._wf_buf = np.roll(self._wf_buf, 1, axis=0)
            self._wf_buf[0] = psd
            self._wf_img.set_data(self._wf_buf)

        iq_tag = " [I/Q✓]" if self._iq_enable else ""
        dm_tag = f" [⊕ {self._demod_mode}]" if self._demod_thread else ""
        self._plot_title.set_text(
            f"{center_hz/1e6:.4f} MHz  ·  BW {srate/1e6:.3f} MHz  ·  "
            f"Gain {gain:.0f} dB  ·  FFT {self._n}  avg×{self._avg}{iq_tag}{dm_tag}")

        freqs  = self._freq_axis(center_hz, srate, self._n)
        pk_idx = int(np.argmax(psd))
        self._statusbar.showMessage(
            f"Center {center_hz/1e6:.4f} MHz  ·  "
            f"Peak {psd[pk_idx]:.1f} dBFS @ {freqs[pk_idx]:.4f} MHz  ·  "
            f"Gain {gain:.0f} dB  ·  BW {srate/1e6:.3f} MSps")
        self._draw_idle()


# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    win = SpectrumWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
