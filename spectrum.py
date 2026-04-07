#!/usr/bin/env python3
"""
SDR Spectrum Analyzer for MSI001/MSI2500-based hardware (SDRplay RSP1).
Uses SoapySDR (miri driver), numpy FFT, matplotlib for real-time display.

Usage:
    python3 spectrum.py [options]

Keyboard controls:
    r       Reset peak hold
    +/-     Increase/decrease gain by 5 dB
    Up/Down Tune center frequency by 100 kHz
    PgUp/Dn Tune center frequency by 1 MHz
    q       Quit
"""

import argparse
import sys
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.ticker import EngFormatter

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
except ImportError:
    sys.exit("SoapySDR Python bindings not found. Install python3-soapysdr.")

# MSI2500 valid sample rates (Hz)
VALID_SRATES = [2_048_000, 2_496_000, 4_000_000, 6_000_000, 8_000_000]

DEFAULT_FREQ  = 100e6     # 100 MHz (FM broadcast band)
DEFAULT_SRATE = 2_048_000
DEFAULT_GAIN  = 30.0      # dB
DEFAULT_FFT   = 2048
DEFAULT_AVG   = 4         # frames to average per display update


def parse_args():
    p = argparse.ArgumentParser(
        description="SDR Spectrum Analyzer — MSI001/MSI2500 (SDRplay RSP1)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-f", "--freq", type=float, default=DEFAULT_FREQ,
                   help="Center frequency (Hz)")
    p.add_argument("-s", "--srate", type=float, default=DEFAULT_SRATE,
                   help=f"Sample rate (Hz). Valid: {[r//1000 for r in VALID_SRATES]} ksps")
    p.add_argument("-g", "--gain", type=float, default=DEFAULT_GAIN,
                   help="Gain (dB)")
    p.add_argument("-n", "--fft-size", type=int, default=DEFAULT_FFT,
                   help="FFT size (power of 2)")
    p.add_argument("--avg", type=int, default=DEFAULT_AVG,
                   help="FFT frames to average per plot update")
    p.add_argument("--driver", default="miri",
                   help="SoapySDR driver string")
    p.add_argument("--ymin", type=float, default=-100, help="Y-axis minimum (dBFS)")
    p.add_argument("--ymax", type=float, default=0,   help="Y-axis maximum (dBFS)")
    return p.parse_args()


class Radio:
    """Wraps SoapySDR device for RX streaming."""

    def __init__(self, driver, freq, srate, gain):
        results = SoapySDR.Device.enumerate(f"driver={driver}")
        if len(results) == 0:
            sys.exit(f"No SoapySDR device found with driver='{driver}'")
        try:
            label = results[0]["label"]
        except Exception:
            label = driver
        print(f"Opening: {label}")

        self.sdr = SoapySDR.Device(f"driver={driver}")

        # Sample rate
        self.sdr.setSampleRate(SOAPY_SDR_RX, 0, srate)
        self.srate = self.sdr.getSampleRate(SOAPY_SDR_RX, 0)

        # Frequency
        self.sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
        self.freq = self.sdr.getFrequency(SOAPY_SDR_RX, 0)

        # Gain (manual mode)
        try:
            self.sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        self._apply_gain(gain)

        print(f"  Sample rate : {self.srate/1e6:.3f} MSps")
        print(f"  Center freq : {self.freq/1e6:.3f} MHz")
        print(f"  Gain        : {self.gain:.1f} dB")

        self.stream = self.sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.sdr.activateStream(self.stream)

    def _apply_gain(self, gain_db):
        try:
            self.sdr.setGain(SOAPY_SDR_RX, 0, gain_db)
            self.gain = self.sdr.getGain(SOAPY_SDR_RX, 0)
        except Exception:
            self.gain = gain_db

    def set_freq(self, freq):
        self.sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
        self.freq = self.sdr.getFrequency(SOAPY_SDR_RX, 0)

    def set_gain(self, gain_db):
        self._apply_gain(np.clip(gain_db, 0, 60))

    def read(self, n):
        """Read exactly n complex64 samples."""
        buf = np.empty(n, dtype=np.complex64)
        received = 0
        while received < n:
            chunk = buf[received:]
            sr = self.sdr.readStream(self.stream, [chunk], len(chunk),
                                     timeoutUs=2_000_000)
            if sr.ret < 0:
                raise RuntimeError(
                    f"readStream error: {SoapySDR.errToStr(sr.ret)}")
            received += sr.ret
        return buf

    def close(self):
        self.sdr.deactivateStream(self.stream)
        self.sdr.closeStream(self.stream)


class SpectrumPlot:
    """Real-time spectrum analyzer display."""

    def __init__(self, radio, args):
        self.radio = radio
        self.fft_size = args.fft_size
        self.avg = args.avg
        self.ymin = args.ymin
        self.ymax = args.ymax

        # Blackman window + its power for normalization
        self.window = np.blackman(self.fft_size).astype(np.float32)
        self.win_power = float(np.mean(self.window ** 2))

        self.peak = np.full(self.fft_size, self.ymin, dtype=np.float32)
        self._build_figure()

    # ------------------------------------------------------------------ #
    # Figure                                                               #
    # ------------------------------------------------------------------ #
    def _build_figure(self):
        self.fig, self.ax = plt.subplots(figsize=(13, 5))
        self.fig.patch.set_facecolor("#111122")
        self.ax.set_facecolor("#0a0a1a")

        freq_mhz = self._freq_axis()
        empty = np.full(self.fft_size, self.ymin, dtype=np.float32)

        self.line, = self.ax.plot(freq_mhz, empty,
                                  color="#00dd88", linewidth=0.7, label="Live")
        self.peak_line, = self.ax.plot(freq_mhz, empty,
                                       color="#ff4455", linewidth=0.5,
                                       alpha=0.65, linestyle="--", label="Peak")

        self.ax.set_ylim(self.ymin, self.ymax)
        self.ax.set_xlim(freq_mhz[0], freq_mhz[-1])
        self.ax.set_xlabel("Frequency (MHz)", color="#999999")
        self.ax.set_ylabel("Power (dBFS)", color="#999999")
        self.ax.tick_params(colors="#999999")
        self.ax.xaxis.set_major_formatter(EngFormatter(unit=""))
        for sp in self.ax.spines.values():
            sp.set_edgecolor("#2a2a44")
        self.ax.grid(True, color="#1a1a33", linewidth=0.5)
        self.ax.legend(facecolor="#111122", labelcolor="#aaaaaa",
                       loc="upper right", fontsize=8)

        self.title = self.ax.set_title("", color="#dddddd", fontsize=10)
        self.info  = self.ax.text(0.01, 0.97, "", transform=self.ax.transAxes,
                                  va="top", color="#777788", fontsize=7.5,
                                  fontfamily="monospace")

        self._update_title()
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def _update_title(self):
        self.title.set_text(
            f"SDR Spectrum  —  {self.radio.freq/1e6:.4f} MHz  |  "
            f"BW {self.radio.srate/1e6:.3f} MHz  |  "
            f"Gain {self.radio.gain:.0f} dB  |  "
            f"FFT {self.fft_size}  avg×{self.avg}"
        )

    def _freq_axis(self):
        freqs = np.fft.fftshift(
            np.fft.fftfreq(self.fft_size, 1.0 / self.radio.srate))
        return (freqs + self.radio.freq) * 1e-6  # → MHz

    # ------------------------------------------------------------------ #
    # Keyboard                                                             #
    # ------------------------------------------------------------------ #
    def _on_key(self, event):
        step_small = 100e3     # 100 kHz
        step_large = 1e6       # 1 MHz
        if event.key == "q":
            plt.close(self.fig)
        elif event.key == "r":
            self.peak[:] = self.ymin
        elif event.key in ("+", "="):
            self.radio.set_gain(self.radio.gain + 5)
            self._update_title()
        elif event.key == "-":
            self.radio.set_gain(self.radio.gain - 5)
            self._update_title()
        elif event.key == "up":
            self.radio.set_freq(self.radio.freq + step_small)
            self._retune()
        elif event.key == "down":
            self.radio.set_freq(self.radio.freq - step_small)
            self._retune()
        elif event.key == "pageup":
            self.radio.set_freq(self.radio.freq + step_large)
            self._retune()
        elif event.key == "pagedown":
            self.radio.set_freq(self.radio.freq - step_large)
            self._retune()

    def _retune(self):
        freq_mhz = self._freq_axis()
        self.line.set_xdata(freq_mhz)
        self.peak_line.set_xdata(freq_mhz)
        self.ax.set_xlim(freq_mhz[0], freq_mhz[-1])
        self.peak[:] = self.ymin
        self._update_title()

    # ------------------------------------------------------------------ #
    # DSP                                                                  #
    # ------------------------------------------------------------------ #
    def _psd_db(self, samples):
        """Single FFT frame → power spectrum in dBFS."""
        frame = samples[: self.fft_size] * self.window
        spec  = np.fft.fftshift(np.fft.fft(frame, self.fft_size))
        psd   = (np.abs(spec) ** 2) / (self.fft_size * self.win_power)
        return 10.0 * np.log10(psd + 1e-30)

    def _averaged_psd(self):
        acc = np.zeros(self.fft_size, dtype=np.float64)
        for _ in range(self.avg):
            acc += self._psd_db(self.radio.read(self.fft_size))
        return (acc / self.avg).astype(np.float32)

    # ------------------------------------------------------------------ #
    # Animation                                                            #
    # ------------------------------------------------------------------ #
    def _update(self, _frame):
        try:
            psd = self._averaged_psd()
        except Exception as exc:
            print(f"[WARN] {exc}", file=sys.stderr)
            return self.line, self.peak_line, self.info

        np.maximum(self.peak, psd, out=self.peak)

        self.line.set_ydata(psd)
        self.peak_line.set_ydata(self.peak)

        peak_idx  = int(np.argmax(psd))
        peak_freq = self._freq_axis()[peak_idx]
        self.info.set_text(
            f"Peak {np.max(psd):.1f} dBFS @ {peak_freq:.4f} MHz  |  "
            f"[↑↓] tune 100 kHz  [PgUp/Dn] 1 MHz  [+/-] gain  [r] reset peak  [q] quit"
        )
        return self.line, self.peak_line, self.info

    def run(self):
        self._ani = animation.FuncAnimation(
            self.fig, self._update, interval=60,
            blit=True, cache_frame_data=False,
        )
        plt.tight_layout()
        plt.show()


def main():
    args = parse_args()

    radio = Radio(
        driver=args.driver,
        freq=args.freq,
        srate=args.srate,
        gain=args.gain,
    )

    plot = SpectrumPlot(radio, args)
    try:
        plot.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("Closing radio...")
        radio.close()


if __name__ == "__main__":
    main()
