import socket
import struct
import threading
import unittest
from pathlib import Path

from radio_listener.config import Config
from radio_listener.dsp import CENTER_HZ, SAMPLE_RATE
from radio_listener.sdr import RtlTcpServer


class FakeProcess:
    def poll(self):
        return None


class SdrTests(unittest.TestCase):
    def test_gmrs_client_retunes_existing_server(self):
        config = Config(
            spool_dir=Path("/tmp"), model_path=Path("/tmp/model"),
            logship_url="https://example.com",
            account_id="11111111-2222-4333-8444-555555555555",
            token="token", gain_db=30.0, ppm=2,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            server = RtlTcpServer(config)
            server.port = listener.getsockname()[1]
            server.process = FakeProcess()
            received = []

            def handle():
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(b"RTL0" + b"\0" * 8)
                    data = b""
                    while len(data) < 25:
                        data += connection.recv(25 - len(data))
                    received.extend(struct.unpack(">BI", data[i:i + 5])
                                    for i in range(0, len(data), 5))

            thread = threading.Thread(target=handle)
            thread.start()
            with server.connect_gmrs():
                pass
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(received, [
                (0x02, SAMPLE_RATE), (0x01, CENTER_HZ), (0x03, 1),
                (0x05, 2), (0x04, 300),
            ])


if __name__ == "__main__":
    unittest.main()
