"""Idempotent Logship blob upload followed by a blob-bearing inflow row."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

from .config import Config


class LogshipClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.account_path = f"/accounts/{config.account_id}"

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
    ) -> bytes:
        request = urllib.request.Request(
            self.config.logship_url + path,
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {token or self.config.token}", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(512).decode("utf-8", errors="replace")
            raise RuntimeError(f"Logship {method} {path} returned HTTP {exc.code}: {detail}") from exc

    def ensure_table(self, mode: str = "gmrs") -> bool:
        """Declare the column when authorized; return False if permission blocks it."""
        if mode == "gmrs":
            schema = self.config.schema
            columns = [{"name": "recording", "type": "BlobReference"}]
        elif mode == "adsb":
            schema = self.config.adsb_schema
            columns = [{"name": "decoded", "type": "Json"}]
        else:
            raise ValueError(f"Unknown mode: {mode}")
        body = json.dumps({"columns": columns}).encode("utf-8")
        try:
            self._request(
                "PUT",
                f"{self.account_path}/tables/{schema}",
                body,
                {"Content-Type": "application/json"},
                token=self.config.admin_token or self.config.token,
            )
            return True
        except RuntimeError as exc:
            if "HTTP 403:" in str(exc) and not self.config.admin_token:
                return False
            raise

    @staticmethod
    def _operation_id(blob_id: str, operation: str) -> str:
        return str(uuid.uuid5(uuid.UUID(blob_id), operation))

    def upload(self, blob_id: str, recording: Path) -> None:
        base = f"{self.account_path}/blobs/uploads/{blob_id}"
        try:
            status = json.loads(self._request("GET", base))
        except RuntimeError as exc:
            if "HTTP 404:" not in str(exc):
                raise
            body = json.dumps({
                "schema": self.config.schema,
                "filename": recording.name,
                "length": recording.stat().st_size,
            }).encode("utf-8")
            self._request("PUT", base, body, {"Content-Type": "application/json"})
            status = json.loads(self._request("GET", base))
        if str(status.get("state", "")).lower() in {"completed", "complete"}:
            return
        committed = int(status.get("committedLength", status.get("length", 0)))
        maximum = int(status["maximumChunkBytes"])
        chunk_bytes = min(64 * 1024, maximum)
        if chunk_bytes <= 0:
            raise RuntimeError("Logship returned an invalid maximumChunkBytes value")
        size = recording.stat().st_size
        if committed > size:
            raise RuntimeError("Logship committed more bytes than the recording contains")
        with recording.open("rb") as handle:
            handle.seek(committed)
            while committed < size:
                chunk = handle.read(min(chunk_bytes, size - committed))
                self._request(
                    "PATCH",
                    f"{base}?offset={committed}",
                    chunk,
                    {
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                        "Idempotency-Key": self._operation_id(blob_id, f"chunk:{committed}:{len(chunk)}"),
                        "X-Blob-SHA256": hashlib.sha256(chunk).hexdigest(),
                    },
                )
                committed += len(chunk)
        digest = hashlib.sha256()
        with recording.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self._request(
            "POST",
            f"{base}/complete",
            b"",
            {
                "Idempotency-Key": self._operation_id(blob_id, "complete"),
                "X-Blob-SHA256": digest.hexdigest(),
            },
        )

    def ingest(self, manifest: dict, transcript: str) -> None:
        event = [{
            "ingestionId": manifest["ingestion_id"],
            "schema": self.config.schema,
            "timestamp": manifest["started_at"],
            "data": {
                "channel": manifest["channel"],
                "frequency_hz": manifest["frequency_hz"],
                "started_at": manifest["started_at"],
                "ended_at": manifest["ended_at"],
                "duration_seconds": manifest["duration_seconds"],
                "transcript": transcript,
                "recording": manifest["blob_id"],
            },
        }]
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        self._request(
            "PUT",
            f"/inflow/{self.config.account_id}",
            body,
            {"Content-Type": "application/json"},
        )

    def ingest_adsb(self, rows: list[dict]) -> None:
        events = [
            {
                "schema": self.config.adsb_schema,
                "timestamp": row["timestamp"],
                "data": row["data"],
            }
            for row in rows
        ]
        self._request(
            "PUT", f"/inflow/{self.config.account_id}",
            json.dumps(events, separators=(",", ":")).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
