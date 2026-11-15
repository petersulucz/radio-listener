from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
import uuid

import numpy as np

from .config import Config
from .dsp import AUDIO_RATE, ChannelAudio, OUTPUT_SAMPLES


LOG = logging.getLogger(__name__)


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def spool_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())


class ChannelRecorder:
    def __init__(self, config: Config, channel: int, frequency_hz: int) -> None:
        self.config = config
        self.channel = channel
        self.frequency_hz = frequency_hz
        pre_blocks = max(1, round(config.pre_roll_seconds * AUDIO_RATE / (OUTPUT_SAMPLES // 2)))
        self.history: deque[tuple[float, np.ndarray]] = deque(maxlen=pre_blocks)
        self.noise_power: float | None = None
        self.calibration_blocks = 0
        self.calibration_powers: list[float] = []
        self.active: dict | None = None
        self.candidate_samples = 0
        self.silence_samples = 0
        self.spool_blocked_until = 0.0

    def process(self, result: ChannelAudio, block_start: float, common_ratio: float = 1.0) -> None:
        audio = result.audio
        power = max(result.power, 1e-14)
        if self.calibration_blocks < 140:
            # Use a median so low-power tuner startup blocks cannot pin the floor too low.
            self.calibration_powers.append(power)
            self.calibration_blocks += 1
            if self.calibration_blocks == 140:
                self.noise_power = float(np.median(self.calibration_powers))
                self.calibration_powers.clear()
        elif self.active is None:
            assert self.noise_power is not None
            rate = 0.03 if power < self.noise_power else 0.003
            self.noise_power += rate * (power - self.noise_power)
        floor = self.noise_power or power
        ratio = power / max(floor, 1e-14)
        carrier = (
            self.calibration_blocks >= 140
            and ratio > 10 ** (self.config.squelch_db / 10)
            and ratio > 2 * max(common_ratio, 1.0)
        )

        self.candidate_samples = self.candidate_samples + len(audio) if carrier else 0
        if self.active is None and self.candidate_samples >= 0.12 * AUDIO_RATE:
            self._open(block_start)
        if self.active is not None:
            self._write(audio)
            self.silence_samples = 0 if carrier else self.silence_samples + len(audio)
            if (
                self.silence_samples >= self.config.tail_seconds * AUDIO_RATE
                or self.active["samples"] >= self.config.max_seconds * AUDIO_RATE
            ):
                self._close(block_start + len(audio) / AUDIO_RATE)
        self.history.append((block_start, audio.copy()))

    def _open(self, block_start: float) -> None:
        if time.monotonic() < self.spool_blocked_until:
            return
        if spool_bytes(self.config.spool_dir) >= self.config.max_spool_bytes:
            LOG.error("Spool is full; skipping channel %s transmission", self.channel)
            self.spool_blocked_until = time.monotonic() + 30
            return
        recording_id = str(uuid.uuid4())
        path = self.config.spool_dir / f"{recording_id}.pcm.part"
        handle = path.open("wb")
        start = self.history[0][0] if self.history else block_start
        self.active = {
            "id": recording_id,
            "path": path,
            "handle": handle,
            "start": start,
            "samples": 0,
        }
        self.silence_samples = 0
        for _, audio in self.history:
            self._write(audio)
        LOG.info(
            "Transmission detected: channel %s (%.4f MHz), recording %s started at %s",
            self.channel, self.frequency_hz / 1_000_000, recording_id, utc_iso(start),
        )

    def _write(self, audio: np.ndarray) -> None:
        if self.active is None:
            return
        pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2")
        self.active["handle"].write(pcm.tobytes())
        self.active["samples"] += len(audio)

    def _close(self, end: float) -> None:
        if self.active is None:
            return
        active = self.active
        self.active = None
        self.candidate_samples = 0
        handle = active["handle"]
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        raw_path = self.config.spool_dir / f"{active['id']}.pcm"
        active["path"].replace(raw_path)
        duration = active["samples"] / AUDIO_RATE
        if duration < self.config.min_seconds:
            raw_path.unlink(missing_ok=True)
            return
        manifest = {
            "id": active["id"],
            "blob_id": str(uuid.uuid4()),
            "ingestion_id": str(uuid.uuid4()),
            "channel": self.channel,
            "frequency_hz": self.frequency_hz,
            "started_at": utc_iso(active["start"]),
            "ended_at": utc_iso(end),
            "duration_seconds": round(duration, 3),
        }
        atomic_json(self.config.spool_dir / f"{active['id']}.json", manifest)
        LOG.info(
            "Transmission recorded: channel %s, recording %s, duration %.2f seconds",
            self.channel, active["id"], duration,
        )

    def finish(self, end: float) -> None:
        self._close(end)
