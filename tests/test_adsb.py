import tempfile
import unittest
from pathlib import Path

from radio_listener.adsb import FRAME, normalize_frame, open_queue


class AdsbTests(unittest.TestCase):
    def test_raw_frame_and_decoded_fields(self):
        frame = "8D406B902015A678D4D220AA4BDA"
        self.assertEqual(FRAME.fullmatch("*" + frame + ";").group(1), frame)
        self.assertIsNone(FRAME.fullmatch("garbage"))
        data = normalize_frame(frame, {"df": 17, "icao": "406B90", "callsign": "EZY85MH"})
        self.assertEqual(data["raw_message"], frame)
        self.assertEqual(data["icao"], "406B90")
        self.assertEqual(data["decoded"]["callsign"], "EZY85MH")

    def test_queue_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "adsb.sqlite"
            connection = open_queue(path)
            connection.execute("INSERT INTO pending VALUES (?, ?, ?)", ("id", "now", "{}"))
            connection.commit()
            connection.close()
            connection = open_queue(path)
            self.assertEqual(connection.execute("SELECT count(*) FROM pending").fetchone()[0], 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()
