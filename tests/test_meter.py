import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from radio_listener.adsb import open_queue
from radio_listener.config import Config
from radio_listener.logship import LogshipClient
from radio_listener.meter import parse_message, scan_meter


class MeterTests(unittest.TestCase):
    def test_parse_scm_and_scmplus(self):
        self.assertEqual(parse_message(json.dumps({
            "Type": "SCM", "Message": {"ID": 123, "Type": 7, "Consumption": 456},
        })), {"meter_id": "123", "protocol": "scm", "consumption_raw": 456,
              "endpoint_type": 7})
        self.assertEqual(parse_message(json.dumps({
            "Type": "SCM+", "Message": {"EndpointID": 123, "EndpointType": 7,
                                       "Consumption": 789},
        })), {"meter_id": "123", "protocol": "scm+", "consumption_raw": 789,
              "endpoint_type": 7})
        self.assertIsNone(parse_message('{"Type":"other","Message":{"ID":123,"Consumption":1}}'))

    def test_queue_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "meter.sqlite"
            connection = open_queue(path)
            connection.execute("INSERT INTO pending VALUES (?, ?, ?)", ("id", "time", "{}"))
            connection.commit()
            connection.close()
            connection = open_queue(path)
            self.assertEqual(connection.execute("SELECT count(*) FROM pending").fetchone()[0], 1)
            connection.close()

    def test_meter_ingestion(self):
        config = Config(
            spool_dir=Path("/tmp"), model_path=Path("/tmp/model"),
            logship_url="https://example.com",
            account_id="11111111-2222-4333-8444-555555555555", token="token",
        )
        class FakeClient(LogshipClient):
            def _request(self, method, path, body=None, headers=None, token=None):
                self.payload = json.loads(body)
                return b""
        client = FakeClient(config)
        client.ingest_meter("reading-id", "2026-11-15T00:00:00Z", {
            "meter_id": "123", "protocol": "scm+", "consumption_raw": 789,
        })
        self.assertEqual(client.payload[0]["schema"], "meters.readings")
        self.assertEqual(client.payload[0]["data"]["meter_id"], "123")

    def test_empty_id_queues_each_meter_once(self):
        messages = [
            {"Type": "SCM", "Message": {"ID": meter_id, "Type": 7, "Consumption": value}}
            for meter_id, value in [(123, 10), (456, 20), (123, 11)]
        ]
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        os.close(write_fd)

        class FakeProcess:
            def __init__(self):
                self.stdout = os.fdopen(read_fd, "rb")
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout=None):
                self.stdout.close()
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary:
            queue_path = Path(temporary) / "meter.sqlite"
            config = Config(
                spool_dir=Path(temporary), model_path=Path(temporary) / "model",
                logship_url="https://example.com",
                account_id="11111111-2222-4333-8444-555555555555", token="token",
                meter_window_seconds=0.5,
            )
            with patch("radio_listener.meter.subprocess.Popen", return_value=FakeProcess()), \
                    patch("radio_listener.meter.select.select", side_effect=lambda streams, *_: (streams, [], [])):
                self.assertTrue(scan_meter(config, threading.Event(), queue_path, 1234))
            connection = open_queue(queue_path)
            rows = connection.execute("SELECT data FROM pending ORDER BY rowid").fetchall()
            connection.close()
            self.assertEqual([json.loads(row[0])["meter_id"] for row in rows], ["123", "456"])


if __name__ == "__main__":
    unittest.main()
