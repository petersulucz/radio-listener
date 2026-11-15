from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
from statistics import median
import subprocess
import sys
import threading
import time

from .channels import CHANNELS_HZ
from .adsb import run_adsb
from .config import load_config
from .dsp import CENTER_HZ, HOP, SAMPLE_RATE, WidebandReceiver
from .logship import LogshipClient
from .recording import ChannelRecorder
from .worker import RecordingWorker


LOG = logging.getLogger(__name__)


def read_exact(stream, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("RTL-SDR stream ended")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous GMRS or 1090 MHz Mode S listener")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("gmrs", "adsb"), default="gmrs")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        if args.mode == "gmrs" and not config.model_path.is_file():
            raise ValueError(f"Whisper model is missing: {config.model_path}")
    except (OSError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    if args.check_config:
        LOG.info("Configuration is valid")
        return 0
    try:
        schema = config.schema if args.mode == "gmrs" else config.adsb_schema
        if LogshipClient(config).ensure_table(args.mode):
            LOG.info("Logship table %s is declared", schema)
        else:
            required = "recording:BlobReference" if args.mode == "gmrs" else "decoded:Json"
            LOG.warning(
                "Token cannot declare Logship table %s; continuing with the existing table. "
                "Ingestion requires its %s column to be declared.", schema, required,
            )
    except Exception:
        LOG.exception("Cannot declare required Logship table")
        return 1
    config.spool_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "adsb":
        return run_adsb(config)
    for orphan in config.spool_dir.glob("*.pcm.part"):
        LOG.warning("Removing incomplete recording from an earlier crash: %s", orphan.name)
        orphan.unlink()
    stop = threading.Event()
    process: subprocess.Popen | None = None

    def shutdown(_signum, _frame) -> None:
        stop.set()
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = RecordingWorker(config, stop)
    worker.start()
    LOG.info("Monitoring channels %s", ", ".join(map(str, CHANNELS_HZ)))
    try:
        while not stop.is_set():
            receiver = WidebandReceiver()
            recorders = {
                channel: ChannelRecorder(config, channel, frequency)
                for channel, frequency in CHANNELS_HZ.items()
            }
            command = [
                "rtl_sdr", "-d", str(config.device_index),
                "-f", str(CENTER_HZ), "-s", str(SAMPLE_RATE),
                "-g", str(config.gain_db), "-p", str(config.ppm), "-",
            ]
            capture_start = time.time()
            samples_read = 0
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE)
                capture_start = time.time()
                assert process.stdout is not None
                while not stop.is_set():
                    raw = read_exact(process.stdout, HOP * 2)
                    block_start = capture_start + samples_read / SAMPLE_RATE
                    samples_read += HOP
                    channels = receiver.process(receiver.decode_iq(raw))
                    ratios = [
                        item.power / max(recorders[item.channel].noise_power or item.power, 1e-14)
                        for item in channels
                    ]
                    common_ratio = median(ratios)
                    for channel_audio in channels:
                        recorders[channel_audio.channel].process(
                            channel_audio, block_start, common_ratio,
                        )
            except (EOFError, OSError, subprocess.SubprocessError) as exc:
                if not stop.is_set():
                    LOG.error("RTL-SDR capture stopped: %s", exc)
            finally:
                for recorder in recorders.values():
                    recorder.finish(capture_start + samples_read / SAMPLE_RATE)
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    process = None
            if not stop.is_set():
                stop.wait(3)
    finally:
        stop.set()
        worker.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
