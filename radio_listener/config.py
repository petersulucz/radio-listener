from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib
import uuid


@dataclass(frozen=True)
class Config:
    spool_dir: Path
    model_path: Path
    logship_url: str
    account_id: str
    token: str
    admin_token: str = ""
    schema: str = "gmrs.transmissions"
    adsb_schema: str = "adsb.messages"
    device_index: int = 0
    gain_db: float = 30.0
    ppm: int = 0
    squelch_db: float = 12.0
    pre_roll_seconds: float = 0.35
    tail_seconds: float = 0.7
    min_seconds: float = 0.25
    max_seconds: float = 120.0
    max_spool_bytes: int = 5_000_000_000


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    radio = data.get("radio", {})
    recording = data.get("recording", {})
    logship = data.get("logship", {})
    token = os.environ.get("LOGSHIP_TOKEN", "")
    if not token:
        raise ValueError("LOGSHIP_TOKEN must be set in the service environment")
    account_id = str(logship["account_id"])
    uuid.UUID(account_id)
    if account_id == str(uuid.UUID(int=0)):
        raise ValueError("Logship blob storage requires a nonzero account UUID")
    url = str(logship["url"]).rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError("logship.url must be an HTTP(S) URL")
    return Config(
        spool_dir=Path(data.get("spool_dir", "/var/lib/radio-listener/spool")),
        model_path=Path(data.get("model_path", "/var/lib/radio-listener/ggml-base.en.bin")),
        logship_url=url,
        account_id=account_id,
        token=token,
        admin_token=os.environ.get("LOGSHIP_ADMIN_TOKEN", ""),
        schema=str(logship.get("schema", "gmrs.transmissions")),
        adsb_schema=str(logship.get("adsb_schema", "adsb.messages")),
        device_index=int(radio.get("device_index", 0)),
        gain_db=float(radio.get("gain_db", 30.0)),
        ppm=int(radio.get("ppm", 0)),
        squelch_db=float(radio.get("squelch_db", 12.0)),
        pre_roll_seconds=float(recording.get("pre_roll_seconds", 0.35)),
        tail_seconds=float(recording.get("tail_seconds", 0.7)),
        min_seconds=float(recording.get("min_seconds", 0.25)),
        max_seconds=float(recording.get("max_seconds", 120.0)),
        max_spool_bytes=int(recording.get("max_spool_bytes", 5_000_000_000)),
    )
