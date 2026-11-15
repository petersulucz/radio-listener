"""1090 MHz Mode S capture, decoding, durable queue, and Logship upload."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from .config import Config
from .logship import LogshipClient


LOG = logging.getLogger(__name__)
FRAME = re.compile(r"^\*([0-9A-Fa-f]{14}|[0-9A-Fa-f]{28});$")
SAMPLE_RATE = 2_400_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def open_queue(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pending ("
        "id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, data TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def normalize_frame(frame: str, decoded: dict) -> dict:
    """Keep common fields queryable while preserving every decoder field."""
    values = dict(decoded)
    data = {
        "raw_message": frame,
        "df": values.get("df", int(frame[:2], 16) >> 3),
        "decoded": values,
    }
    for field in (
        "icao", "icao_verified", "typecode", "bds", "callsign", "squawk",
        "latitude", "longitude", "altitude", "groundspeed", "track",
        "vertical_rate", "crc_valid",
    ):
        value = values.get(field)
        if value is not None:
            data[field] = value
    return data


class AdsbUploader(threading.Thread):
    def __init__(self, config: Config, queue_path: Path, stop: threading.Event) -> None:
        super().__init__(name="adsb-uploader", daemon=True)
        self.client = LogshipClient(config)
        self.queue_path = queue_path
        self.stop = stop

    def run(self) -> None:
        connection = open_queue(self.queue_path)
        failures = 0
        try:
            while not self.stop.is_set():
                batch = connection.execute(
                    "SELECT id, timestamp, data FROM pending ORDER BY rowid LIMIT 100"
                ).fetchall()
                if not batch:
                    self.stop.wait(1)
                    continue
                if len(batch) < 100:
                    self.stop.wait(1)
                    if self.stop.is_set():
                        break
                    batch = connection.execute(
                        "SELECT id, timestamp, data FROM pending ORDER BY rowid LIMIT 100"
                    ).fetchall()
                try:
                    self.client.ingest_adsb([
                        {"timestamp": timestamp, "data": json.loads(data)}
                        for _, timestamp, data in batch
                    ])
                    connection.executemany("DELETE FROM pending WHERE id = ?", [(id_,) for id_, _, _ in batch])
                    connection.commit()
                    failures = 0
                    LOG.info("Uploaded %d Mode S messages to Logship", len(batch))
                except Exception:
                    failures += 1
                    delay = min(300, 5 * 2 ** min(failures - 1, 6))
                    LOG.exception("ADS-B upload failed; retrying in %d seconds", delay)
                    self.stop.wait(delay)
        finally:
            connection.close()


def run_adsb(config: Config) -> int:
    from pyModeS import PipeDecoder

    queue_path = config.spool_dir / "adsb.sqlite"
    connection = open_queue(queue_path)
    stop = threading.Event()
    uploader = AdsbUploader(config, queue_path, stop)
    uploader.start()
    processes: list[subprocess.Popen] = []

    def shutdown(_signum, _frame) -> None:
        stop.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    last_report = time.monotonic()
    last_overflow = 0.0
    received = 0
    LOG.info("Monitoring 1090 MHz Mode S messages")
    try:
        while not stop.is_set():
            decoder = PipeDecoder()
            rtl = None
            dump = None
            try:
                rtl = subprocess.Popen(
                    ["rtl_sdr", "-d", str(config.device_index), "-f", "1090000000",
                     "-s", str(SAMPLE_RATE), "-g", str(config.gain_db),
                     "-p", str(config.ppm), "-"],
                    stdout=subprocess.PIPE,
                )
                assert rtl.stdout is not None
                dump = subprocess.Popen(
                    ["dump1090", "--ifile", "-", "--iformat", "UC8", "--raw"],
                    stdin=rtl.stdout, stdout=subprocess.PIPE, text=True, bufsize=1,
                )
                rtl.stdout.close()
                processes[:] = [rtl, dump]
                assert dump.stdout is not None
                for line in dump.stdout:
                    if stop.is_set():
                        break
                    match = FRAME.fullmatch(line.strip())
                    if not match:
                        continue
                    frame = match.group(1).upper()
                    stamp = utc_now()
                    timestamp = time.time()
                    try:
                        decoded = dict(decoder.decode(frame, timestamp=timestamp))
                    except Exception as exc:
                        LOG.warning("Could not decode Mode S frame %s: %s", frame, exc)
                        decoded = {"error": str(exc)}
                    data = normalize_frame(frame, decoded)
                    received += 1
                    now = time.monotonic()
                    if now - last_report >= 60:
                        pending = connection.execute("SELECT count(*) FROM pending").fetchone()[0]
                        LOG.info("Mode S messages: %d received in interval; %d queued", received, pending)
                        received = 0
                        last_report = now
                    size = sum(p.stat().st_size for p in (
                        queue_path, queue_path.with_name(queue_path.name + "-wal"),
                    ) if p.exists())
                    if size >= config.max_spool_bytes:
                        if now - last_overflow >= 60:
                            LOG.error("ADS-B queue is full (%d bytes); dropping new messages", size)
                            last_overflow = now
                        continue
                    connection.execute(
                        "INSERT INTO pending (id, timestamp, data) VALUES (?, ?, ?)",
                        (str(uuid.uuid4()), stamp, json.dumps(data, separators=(",", ":"))),
                    )
                    connection.commit()
                if not stop.is_set():
                    LOG.error("ADS-B decoder stream ended; restarting")
            except (OSError, subprocess.SubprocessError) as exc:
                if not stop.is_set():
                    LOG.error("ADS-B capture stopped: %s", exc)
            finally:
                for process in (dump, rtl):
                    if process is None:
                        continue
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                processes.clear()
            if not stop.is_set():
                stop.wait(3)
    finally:
        stop.set()
        uploader.join(timeout=10)
        connection.close()
    return 0
