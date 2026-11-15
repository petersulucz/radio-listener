"""Offline IQ diagnostic for checking channelization and processing speed."""

from __future__ import annotations

import argparse
import time
import numpy as np

from .channels import CHANNELS_HZ
from .dsp import HOP, SAMPLE_RATE, WidebandReceiver


def main() -> int:
    parser = argparse.ArgumentParser(description="Process an rtl_sdr IQ capture")
    parser.add_argument("capture", type=argparse.FileType("rb"))
    args = parser.parse_args()
    receiver = WidebandReceiver()
    powers = {channel: [] for channel in CHANNELS_HZ}
    blocks = 0
    start = time.monotonic()
    while raw := args.capture.read(HOP * 2):
        if len(raw) != HOP * 2:
            break
        for result in receiver.process(receiver.decode_iq(raw)):
            powers[result.channel].append(result.power)
        blocks += 1
    elapsed = time.monotonic() - start
    capture_seconds = blocks * HOP / SAMPLE_RATE
    print(f"Processed {capture_seconds:.2f} seconds of IQ in {elapsed:.2f} seconds")
    for channel, frequency in CHANNELS_HZ.items():
        values = np.asarray(powers[channel])
        first_second = values[:round(SAMPLE_RATE / HOP)]
        later = values[len(first_second):]
        baseline = float(np.median(first_second)) if len(first_second) else 0.0
        median = float(np.median(later)) if len(later) else 0.0
        peak = float(np.max(later)) if len(later) else 0.0
        estimate = None
        ratios = []
        for power in values:
            power = max(float(power), 1e-14)
            if estimate is None:
                estimate = power
            rate = 0.03 if power < estimate else 0.003
            estimate += rate * (power - estimate)
            ratios.append(power / max(estimate, 1e-14))
        post_calibration = ratios[141:]
        print(
            f"Channel {channel:2d} {frequency / 1e6:.4f} MHz  "
            f"first-s median {baseline:.3e}  later median {median:.3e}  "
            f"later peak {peak:.3e}  median/baseline {median / max(baseline, 1e-14):.1f}x  "
            f"max/old-floor {max(post_calibration, default=0):.1f}x"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
