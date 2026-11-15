from __future__ import annotations

import argparse
import logging
from pathlib import Path
import select
import signal
import socket
from statistics import median
import subprocess
import sys
import threading
import time

from .channels import CHANNELS_HZ
from .adsb import run_adsb
from .config import load_config
from .dsp import HOP, SAMPLE_RATE, WidebandReceiver
from .logship import LogshipClient
from .meter import MeterUploader, scan_meter
from .recording import ChannelRecorder
from .sdr import RtlTcpServer
from .worker import RecordingWorker


LOG = logging.getLogger(__name__)


def read_until(stream: socket.socket, count: int, deadline: float | None,
               stop: threading.Event) -> bytes | None:
    chunks = []
    remaining = count
    while remaining and not stop.is_set():
        timeout = 0.25 if deadline is None else min(0.25, max(0, deadline - time.monotonic()))
        if deadline is not None and timeout <= 0:
            return None
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            continue
        chunk = stream.recv(remaining)
        if not chunk:
            raise EOFError("RTL-SDR stream ended")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks) if remaining == 0 else None


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
        client = LogshipClient(config)
        for mode, schema, required in (
            [("gmrs", config.schema, "channel:Int64, frequency_hz:Int64, recording:BlobReference, started_at:DateTime, ended_at:DateTime, duration_seconds:Float64, transcript:String"),
             ("meter", config.meter_schema, "meter_id:String and consumption_raw:UInt64 columns")]
            if args.mode == "gmrs" and config.meter_enabled else
            [(args.mode, config.schema if args.mode == "gmrs" else config.adsb_schema,
              "channel:Int64, frequency_hz:Int64, recording:BlobReference, started_at:DateTime, ended_at:DateTime, duration_seconds:Float64, transcript:String"
              if args.mode == "gmrs" else "decoded:Json")]
        ):
            if client.ensure_table(mode):
                LOG.info("Logship table %s is declared", schema)
            else:
                LOG.warning("Token cannot declare Logship table %s; continuing with the existing table. "
                            "Ingestion requires its %s to exist.", schema, required)
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
    server = RtlTcpServer(config)
    client_socket: socket.socket | None = None

    def shutdown(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = RecordingWorker(config, stop)
    worker.start()
    meter_queue = config.spool_dir / "meter.sqlite"
    meter_uploader = MeterUploader(config, stop, meter_queue) if config.meter_enabled else None
    if meter_uploader is not None:
        meter_uploader.start()
    next_scan = time.monotonic() + config.meter_interval_seconds
    LOG.info("Monitoring channels %s", ", ".join(map(str, CHANNELS_HZ)))
    try:
        while not stop.is_set():
            if server.process is None or server.process.poll() is not None:
                server.stop()
                try:
                    server.start()
                    LOG.info("Started persistent RTL-SDR server on loopback")
                except OSError as exc:
                    LOG.error("Could not start RTL-SDR server: %s", exc)
                    stop.wait(3)
                    continue
            if config.meter_enabled and time.monotonic() >= next_scan:
                next_scan = time.monotonic() + config.meter_interval_seconds
                try:
                    scan_meter(config, stop, meter_queue, server.port)
                except (OSError, subprocess.SubprocessError) as exc:
                    if not stop.is_set():
                        LOG.error("Meter scan failed: %s", exc)
                if not stop.is_set():
                    LOG.info("Returning to GMRS monitoring")
                continue
            receiver = WidebandReceiver()
            recorders = {
                channel: ChannelRecorder(config, channel, frequency)
                for channel, frequency in CHANNELS_HZ.items()
            }
            capture_start = time.time()
            samples_read = 0
            capture_failed = False
            try:
                client_socket = server.connect_gmrs()
                capture_start = time.time()
                while not stop.is_set():
                    raw = read_until(client_socket, HOP * 2,
                                     next_scan if config.meter_enabled else None, stop)
                    if raw is None:
                        break
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
                capture_failed = True
                if not stop.is_set():
                    LOG.error("RTL-SDR capture stopped: %s", exc)
            finally:
                for recorder in recorders.values():
                    recorder.finish(capture_start + samples_read / SAMPLE_RATE)
                if client_socket is not None:
                    client_socket.close()
                    client_socket = None
            if not stop.is_set() and capture_failed:
                stop.wait(3)
    finally:
        stop.set()
        server.stop()
        worker.join(timeout=10)
        if meter_uploader is not None:
            meter_uploader.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
