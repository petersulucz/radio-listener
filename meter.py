"""Short SCM/SCM+ meter scans between GMRS capture windows."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import select
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .adsb import open_queue
from .config import Config
from .logship import LogshipClient


LOG = logging.getLogger(__name__)


def parse_message(line: str) -> dict | None:
    try:
        packet = json.loads(line)
        protocol = packet["Type"].lower()
        if protocol not in {"scm", "scm+"}:
            return None
        message = packet["Message"]
        meter_id = str(message["ID" if protocol == "scm" else "EndpointID"])
        consumption = int(message["Consumption"])
        if not meter_id.isdecimal() or consumption < 0:
            return None
        return {
            "meter_id": meter_id,
            "protocol": protocol,
            "consumption_raw": consumption,
            "endpoint_type": message.get("Type" if protocol == "scm" else "EndpointType"),
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def scan_meter(config: Config, stop: threading.Event, queue_path: Path, server_port: int) -> bool:
    """Queue one reading per ID in upload-all mode, or the configured endpoint."""
    deadline = time.monotonic() + config.meter_window_seconds
    decoder = None
    seen: set[str] = set()
    found = False
    queued = 0
    LOG.info("Switching to meter scan for up to %.0f seconds", config.meter_window_seconds)
    if not config.meter_id:
        LOG.info("Meter scan will upload one reading per decoded ID during this window")
    try:
        decoder = subprocess.Popen(
            ["rtlamr", f"-server=127.0.0.1:{server_port}", "-msgtype=scm,scm+",
             "-format=json", "-centerfreq=912600155", "-samplerate=2359296",
             f"-tunergain={config.gain_db}",
             f"-tunergainmode={str(config.gain_db != 0).lower()}",
             f"-freqcorrection={config.ppm}"],
            stdout=subprocess.PIPE,
        )
        assert decoder.stdout is not None
        buffer = b""
        while not stop.is_set() and time.monotonic() < deadline:
            if decoder.poll() is not None:
                LOG.warning("Meter decoder exited with status %s", decoder.returncode)
                break
            ready, _, _ = select.select([decoder.stdout], [], [], min(0.25, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            chunk = os.read(decoder.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                reading = parse_message(line.decode("utf-8", errors="replace"))
                if reading is None:
                    continue
                meter_id = reading["meter_id"]
                first_seen = meter_id not in seen
                if first_seen:
                    LOG.info("Meter candidate: ID %s, %s, raw consumption %s", meter_id,
                             reading["protocol"], reading["consumption_raw"])
                    seen.add(meter_id)
                if not config.meter_id:
                    if not first_seen:
                        continue
                elif meter_id != config.meter_id:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                connection = open_queue(queue_path)
                try:
                    connection.execute(
                        "INSERT INTO pending (id, timestamp, data) VALUES (?, ?, ?)",
                        (str(uuid.uuid4()), timestamp, json.dumps(reading, separators=(",", ":"))),
                    )
                    connection.commit()
                finally:
                    connection.close()
                LOG.info("Meter reading: ID %s, %s, raw consumption %s", meter_id,
                         reading["protocol"], reading["consumption_raw"])
                queued += 1
                found = True
                if config.meter_id:
                    break
            if found and config.meter_id:
                break
        if not config.meter_id:
            LOG.info("Meter window ended; queued %d readings from %d candidate IDs", queued, len(seen))
        elif not found:
            if config.meter_id:
                LOG.info("Meter window ended without a reading for ID %s; decoded %d candidate IDs",
                         config.meter_id, len(seen))
            else:
                LOG.info("Meter discovery window ended; decoded %d candidate IDs", len(seen))
        return found
    finally:
        _terminate(decoder)


class MeterUploader(threading.Thread):
    def __init__(self, config: Config, stop: threading.Event, queue_path: Path) -> None:
        super().__init__(name="meter-uploader", daemon=True)
        self.client = LogshipClient(config)
        self.stop = stop
        self.queue_path = queue_path

    def run(self) -> None:
        connection = open_queue(self.queue_path)
        failures = 0
        try:
            while not self.stop.is_set():
                row = connection.execute(
                    "SELECT id, timestamp, data FROM pending ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    self.stop.wait(2)
                    continue
                try:
                    self.client.ingest_meter(row[0], row[1], json.loads(row[2]))
                    connection.execute("DELETE FROM pending WHERE id = ?", (row[0],))
                    connection.commit()
                    failures = 0
                    LOG.info("Uploaded meter reading %s", row[0])
                except Exception:
                    failures += 1
                    delay = min(300, 5 * 2 ** min(failures - 1, 6))
                    LOG.exception("Meter upload failed; retrying in %d seconds", delay)
                    self.stop.wait(delay)
        finally:
            connection.close()
