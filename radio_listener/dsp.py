"""One FFT-based narrowband FM receiver for every 462 MHz GMRS channel.

The RTL sample rate and FFT size put every selected channel on an exact FFT
bin. One wideband transform is shared by all 15 independent receivers.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from scipy.signal import lfilter

from .channels import CHANNELS_HZ


SAMPLE_RATE = 1_024_000
CENTER_HZ = 462_450_000  # Keep the tuner DC spike outside the monitored band.
FFT_SIZE = 8192
OVERLAP = 1024
HOP = FFT_SIZE - OVERLAP
DECIMATION = 32
CHANNEL_RATE = SAMPLE_RATE // DECIMATION
AUDIO_RATE = CHANNEL_RATE // 2
OUTPUT_SAMPLES = HOP // DECIMATION


def _filter_response(cutoff_hz: float) -> np.ndarray:
    taps = OVERLAP + 1
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    impulse = 2 * cutoff_hz / SAMPLE_RATE * np.sinc(2 * cutoff_hz * n / SAMPLE_RATE)
    impulse *= np.kaiser(taps, 8.6)
    impulse /= impulse.sum()
    response = np.fft.fft(impulse, FFT_SIZE)
    base_bins = np.arange(FFT_SIZE // DECIMATION, dtype=np.int64)
    base_bins[FFT_SIZE // DECIMATION // 2:] += FFT_SIZE - FFT_SIZE // DECIMATION
    return response[base_bins]


@dataclass
class ChannelAudio:
    channel: int
    frequency_hz: int
    audio: np.ndarray
    power: float


class _Demodulator:
    def __init__(self) -> None:
        self.previous_iq = 1 + 0j
        self.deemphasis_zi = np.zeros(1)
        self.highpass_zi = np.zeros(1)
        self.deemphasis_alpha = 1 - np.exp(-1 / (CHANNEL_RATE * 75e-6))
        self.highpass_alpha = np.exp(-2 * np.pi * 300 / CHANNEL_RATE)

    def process(self, samples: np.ndarray) -> np.ndarray:
        previous = np.empty_like(samples)
        previous[0] = self.previous_iq
        previous[1:] = samples[:-1]
        self.previous_iq = complex(samples[-1])
        # FM discriminator, scaled for nominal 5 kHz peak deviation.
        phase = np.angle(samples * np.conj(previous))
        demodulated = phase * CHANNEL_RATE / (2 * np.pi * 5_000)
        deemphasized, self.deemphasis_zi = lfilter(
            [self.deemphasis_alpha], [1, -(1 - self.deemphasis_alpha)],
            demodulated, zi=self.deemphasis_zi,
        )
        highpassed, self.highpass_zi = lfilter(
            [self.highpass_alpha, -self.highpass_alpha],
            [1, -self.highpass_alpha], deemphasized, zi=self.highpass_zi,
        )
        return np.clip(highpassed[::2] * 0.75, -1, 1).astype(np.float32)


class WidebandReceiver:
    def __init__(self) -> None:
        self._overlap = np.zeros(OVERLAP, dtype=np.complex64)
        self._sample_index = 0
        self._base_bins = np.arange(FFT_SIZE // DECIMATION, dtype=np.int64)
        self._base_bins[FFT_SIZE // DECIMATION // 2:] += FFT_SIZE - FFT_SIZE // DECIMATION
        self._filters = {
            6_000: _filter_response(6_000),
            8_500: _filter_response(8_500),
        }
        self._demodulators = {channel: _Demodulator() for channel in CHANNELS_HZ}
        self._bin_offsets = {}
        for channel, frequency in CHANNELS_HZ.items():
            offset, remainder = divmod((frequency - CENTER_HZ) * FFT_SIZE, SAMPLE_RATE)
            if remainder:
                raise ValueError(f"Channel {channel} is not aligned to an FFT bin")
            self._bin_offsets[channel] = offset

    @staticmethod
    def decode_iq(raw: bytes) -> np.ndarray:
        values = np.frombuffer(raw, dtype=np.uint8)
        if len(values) != HOP * 2:
            raise ValueError(f"Expected {HOP * 2} IQ bytes, got {len(values)}")
        scaled = (values.astype(np.float32) - 127.5) / 127.5
        return scaled[::2] + 1j * scaled[1::2]

    def process(self, iq: np.ndarray) -> list[ChannelAudio]:
        if len(iq) != HOP:
            raise ValueError(f"Expected {HOP} IQ samples")
        frame = np.concatenate((self._overlap, iq))
        spectrum = np.fft.fft(frame)
        frame_start = self._sample_index - OVERLAP
        self._sample_index += HOP
        self._overlap = iq[-OVERLAP:].copy()
        output = []
        for channel, frequency in CHANNELS_HZ.items():
            bin_offset = self._bin_offsets[channel]
            bins = (self._base_bins + bin_offset) % FFT_SIZE
            response = self._filters[6_000 if channel <= 7 else 8_500]
            shifted = spectrum[bins] * response
            baseband = np.fft.ifft(shifted) / DECIMATION
            phase = np.exp(-2j * np.pi * ((bin_offset * frame_start) % FFT_SIZE) / FFT_SIZE)
            baseband = baseband[OVERLAP // DECIMATION:] * phase
            audio = self._demodulators[channel].process(baseband)
            output.append(ChannelAudio(channel, frequency, audio, float(np.mean(np.abs(baseband) ** 2))))
        return output
