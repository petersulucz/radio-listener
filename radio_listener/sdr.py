"""Keep one RTL-SDR device open while clients switch frequencies."""

from __future__ import annotations

import socket
import struct
import subprocess
import time

from .config import Config
from .dsp import CENTER_HZ, SAMPLE_RATE


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _header(sock: socket.socket) -> bytes:
    data = b""
    while len(data) < 12:
        chunk = sock.recv(12 - len(data))
        if not chunk:
            raise ConnectionError("rtl_tcp closed before its device header")
        data += chunk
    if data[:4] != b"RTL0":
        raise ConnectionError("rtl_tcp sent an invalid device header")
    return data


class RtlTcpServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.port = _unused_port()
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            ["rtl_tcp", "-a", "127.0.0.1", "-p", str(self.port),
             "-d", str(self.config.device_index), "-f", str(CENTER_HZ),
             "-s", str(SAMPLE_RATE), "-g", str(self.config.gain_db),
             "-P", str(self.config.ppm)],
            stdout=subprocess.DEVNULL,
        )

    def connect_gmrs(self) -> socket.socket:
        if self.process is None:
            raise RuntimeError("rtl_tcp server is not started")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise ConnectionError(f"rtl_tcp exited with status {self.process.returncode}")
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=0.5)
                try:
                    sock.settimeout(2)
                    _header(sock)
                    commands = [
                        (0x02, SAMPLE_RATE),
                        (0x01, CENTER_HZ),
                        (0x03, 0 if self.config.gain_db == 0 else 1),
                        (0x05, self.config.ppm & 0xFFFFFFFF),
                    ]
                    if self.config.gain_db != 0:
                        commands.append((0x04, round(self.config.gain_db * 10)))
                    sock.sendall(b"".join(struct.pack(">BI", key, value) for key, value in commands))
                    sock.settimeout(None)
                    return sock
                except Exception:
                    sock.close()
                    raise
            except (OSError, ConnectionError):
                time.sleep(0.1)
        raise ConnectionError("rtl_tcp did not accept a GMRS client within 5 seconds")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
