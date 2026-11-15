from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess
import threading
import time
import wave

from .config import Config
from .dsp import AUDIO_RATE
from .logship import LogshipClient


LOG = logging.getLogger(__name__)


class RecordingWorker(threading.Thread):
    def __init__(self, config: Config, stop: threading.Event) -> None:
        super().__init__(name="recording-worker", daemon=True)
        self.config = config
        self.stop = stop
        self.client = LogshipClient(config)
        self.next_attempt: dict[str, float] = {}
        self.failures: dict[str, int] = {}

    def run(self) -> None:
        while not self.stop.is_set():
            for done_path in self.config.spool_dir.glob("*.done"):
                try:
                    self._cleanup(done_path)
                except OSError:
                    LOG.exception("Could not remove completed recording %s", done_path.stem)
            manifests = sorted(
                self.config.spool_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
            )
            for manifest_path in manifests:
                if self.stop.is_set():
                    return
                key = manifest_path.stem
                if time.monotonic() < self.next_attempt.get(key, 0):
                    continue
                try:
                    self._process(manifest_path)
                    self.next_attempt.pop(key, None)
                    self.failures.pop(key, None)
                except Exception:
                    count = self.failures.get(key, 0) + 1
                    self.failures[key] = count
                    delay = min(300, 5 * 2 ** min(count - 1, 6))
                    self.next_attempt[key] = time.monotonic() + delay
                    LOG.exception("Processing %s failed; retrying in %s seconds", key, delay)
            self.stop.wait(2)

    def _process(self, manifest_path: Path) -> None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stem = manifest_path.with_suffix("")
        raw_path = stem.with_suffix(".pcm")
        mp3_path = stem.with_suffix(".mp3")
        wav_path = stem.with_suffix(".wav")
        transcript_path = stem.with_suffix(".txt")
        whisper_prefix = stem.with_suffix(".whisper")
        if not mp3_path.exists():
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1",
                    "-i", str(raw_path), "-codec:a", "libmp3lame", "-b:a", "64k",
                    "-f", "mp3",
                    str(mp3_path.with_suffix(".mp3.part")),
                ],
                check=True,
                capture_output=True,
            )
            mp3_path.with_suffix(".mp3.part").replace(mp3_path)
        if not transcript_path.exists():
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(AUDIO_RATE)
                output.writeframes(raw_path.read_bytes())
            subprocess.run(
                [
                    "whisper-cli", "-m", str(self.config.model_path),
                    "-f", str(wav_path), "-l", "en", "-otxt",
                    "-of", str(whisper_prefix),
                ],
                check=True,
                capture_output=True,
            )
            whisper_prefix.with_suffix(".whisper.txt").replace(transcript_path)
            transcript = transcript_path.read_text(encoding="utf-8").strip()
            LOG.info(
                "Transcription complete: channel %s, recording %s, text: %s",
                manifest["channel"], manifest["id"],
                " ".join(transcript.split()) or "(empty)",
            )
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        self.client.upload(manifest["blob_id"], mp3_path)
        self.client.ingest(manifest, transcript)
        done_path = stem.with_suffix(".done")
        manifest_path.replace(done_path)
        self._cleanup(done_path)
        LOG.info("Uploaded channel %s recording %s", manifest["channel"], manifest["id"])

    @staticmethod
    def _cleanup(done_path: Path) -> None:
        stem = done_path.with_suffix("")
        for path in (
            stem.with_suffix(".pcm"),
            stem.with_suffix(".mp3"),
            stem.with_suffix(".wav"),
            stem.with_suffix(".txt"),
            stem.with_suffix(".whisper.txt"),
            stem.with_suffix(".mp3.part"),
            done_path,
        ):
            path.unlink(missing_ok=True)
