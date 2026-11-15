import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from radio_listener.config import Config
from radio_listener.logship import LogshipClient


class FakeLogship(LogshipClient):
    def __init__(self, config):
        super().__init__(config)
        self.committed = bytearray()
        self.completed = False
        self.events = []
        self.patch_count = 0
        self.table_token = None
        self.table_forbidden = False
        self.table_body = None

    def _request(self, method, path, body=None, headers=None, token=None):
        if "/tables/" in path:
            assert method == "PUT"
            if self.table_forbidden:
                raise RuntimeError("Logship PUT table returned HTTP 403: Forbidden")
            self.table_token = token
            self.table_body = json.loads(body)
            return b""
        if path.endswith("/complete"):
            self.assert_digest(headers["X-Blob-SHA256"])
            self.completed = True
            return b""
        if "/blobs/uploads/" in path:
            if method == "GET":
                return json.dumps({
                    "state": "completed" if self.completed else "uploading",
                    "length": len(self.committed),
                    "maximumChunkBytes": 1024,
                }).encode()
            if method == "PATCH":
                offset = int(path.split("offset=")[1])
                assert offset == len(self.committed)
                assert hashlib.sha256(body).hexdigest() == headers["X-Blob-SHA256"]
                self.committed.extend(body)
                self.patch_count += 1
                return b""
        if path.startswith("/inflow/"):
            self.events.extend(json.loads(body))
            return b""
        raise AssertionError((method, path))

    def assert_digest(self, actual):
        assert hashlib.sha256(self.committed).hexdigest() == actual


class LogshipTests(unittest.TestCase):
    def test_startup_declares_blob_column_with_management_token(self):
        config = Config(
            spool_dir=Path("/tmp"),
            model_path=Path("/tmp/model.bin"),
            logship_url="https://example.com",
            account_id="11111111-2222-4333-8444-555555555555",
            token="inflow-token",
            admin_token="management-token",
        )
        client = FakeLogship(config)
        self.assertTrue(client.ensure_table())
        self.assertEqual(client.table_token, "management-token")
        self.assertEqual(client.table_body, {"columns": [
            {"name": "recording", "type": "BlobReference"},
            {"name": "started_at", "type": "DateTime"},
            {"name": "ended_at", "type": "DateTime"},
            {"name": "duration_seconds", "type": "Float64"},
        ]})

    def test_adsb_table_and_batch(self):
        config = Config(
            spool_dir=Path("/tmp"), model_path=Path("/tmp/model.bin"),
            logship_url="https://example.com",
            account_id="11111111-2222-4333-8444-555555555555",
            token="inflow-token",
        )
        client = FakeLogship(config)
        self.assertTrue(client.ensure_table("adsb"))
        self.assertEqual(client.table_body, {"columns": [{"name": "decoded", "type": "Json"}]})
        client.ingest_adsb([{
            "timestamp": "2026-11-14T00:00:00Z",
            "data": {"raw_message": "8D406B902015A678D4D220AA4BDA", "decoded": {"df": 17}},
        }])
        self.assertEqual(client.events[0]["schema"], "adsb.messages")
        self.assertEqual(client.events[0]["data"]["decoded"]["df"], 17)

    def test_inflow_only_token_does_not_block_capture_of_existing_table(self):
        config = Config(
            spool_dir=Path("/tmp"),
            model_path=Path("/tmp/model.bin"),
            logship_url="https://example.com",
            account_id="11111111-2222-4333-8444-555555555555",
            token="inflow-token",
        )
        client = FakeLogship(config)
        client.table_forbidden = True
        self.assertFalse(client.ensure_table())

    def test_upload_resume_and_blob_reference_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recording.mp3"
            path.write_bytes(b"audio" * 500)
            config = Config(
                spool_dir=Path(temporary),
                model_path=Path(temporary) / "model.bin",
                logship_url="https://example.com",
                account_id="11111111-2222-4333-8444-555555555555",
                token="test",
            )
            client = FakeLogship(config)
            blob_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            client.upload(blob_id, path)
            patches = client.patch_count
            client.upload(blob_id, path)
            self.assertEqual(client.patch_count, patches)
            self.assertEqual(bytes(client.committed), path.read_bytes())
            client.ingest({
                "id": "recording-id",
                "blob_id": blob_id,
                "ingestion_id": "11111111-2222-4333-8444-555555555555",
                "channel": 1,
                "frequency_hz": 462_562_500,
                "started_at": "2026-11-14T00:00:00Z",
                "ended_at": "2026-11-14T00:00:01Z",
                "duration_seconds": 1,
            }, "hello")
            self.assertEqual(client.events[0]["data"]["recording"], blob_id)
            self.assertEqual(client.events[0]["data"]["transcript"], "hello")


if __name__ == "__main__":
    unittest.main()
