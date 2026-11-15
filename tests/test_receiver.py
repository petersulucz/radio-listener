import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from radio_listener.channels import CHANNELS_HZ
from radio_listener.config import Config
from radio_listener.dsp import AUDIO_RATE, CENTER_HZ, HOP, SAMPLE_RATE, ChannelAudio, WidebandReceiver
from radio_listener.recording import ChannelRecorder


class ReceiverTests(unittest.TestCase):
    def test_one_iq_stream_separates_distant_simultaneous_channels(self):
        receiver = WidebandReceiver()
        n = np.arange(HOP * 4)
        first = np.exp(2j * np.pi * (CHANNELS_HZ[1] - CENTER_HZ) * n / SAMPLE_RATE)
        second = np.exp(2j * np.pi * (CHANNELS_HZ[22] - CENTER_HZ) * n / SAMPLE_RATE)
        samples = 0.2 * first + 0.2 * second
        result = None
        for block in np.split(samples, 4):
            result = {item.channel: item for item in receiver.process(block)}
        self.assertEqual(len(result), 15)
        self.assertEqual(len(result[1].audio), HOP * AUDIO_RATE // SAMPLE_RATE)
        self.assertGreater(result[1].power, result[19].power * 20)
        self.assertGreater(result[22].power, result[19].power * 20)

    def test_recorder_queues_complete_transmission(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = Config(
                spool_dir=folder,
                model_path=folder / "model.bin",
                logship_url="https://example.com",
                account_id="11111111-2222-4333-8444-555555555555",
                token="test",
                min_seconds=0.1,
            )
            recorder = ChannelRecorder(config, 1, CHANNELS_HZ[1])
            block = np.zeros(HOP * AUDIO_RATE // SAMPLE_RATE, dtype=np.float32)
            t = 0.0
            for _ in range(150):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-8), t)
                t += len(block) / AUDIO_RATE
            for _ in range(20):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-4), t)
                t += len(block) / AUDIO_RATE
            for _ in range(120):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-8), t)
                t += len(block) / AUDIO_RATE
            manifests = list(folder.glob("*.json"))
            self.assertEqual(len(manifests), 1)
            metadata = json.loads(manifests[0].read_text())
            self.assertEqual(metadata["channel"], 1)
            self.assertTrue((folder / f"{metadata['id']}.pcm").exists())

    def test_common_power_rise_and_short_spike_do_not_open_squelch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = Config(
                spool_dir=folder,
                model_path=folder / "model.bin",
                logship_url="https://example.com",
                account_id="11111111-2222-4333-8444-555555555555",
                token="test",
            )
            recorder = ChannelRecorder(config, 1, CHANNELS_HZ[1])
            block = np.zeros(HOP * AUDIO_RATE // SAMPLE_RATE, dtype=np.float32)
            t = 0.0
            for _ in range(150):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-8), t)
                t += len(block) / AUDIO_RATE
            for _ in range(30):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-5), t, common_ratio=1000)
                t += len(block) / AUDIO_RATE
            self.assertIsNone(recorder.active)
            for _ in range(10):
                recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-4), t)
                t += len(block) / AUDIO_RATE
            recorder.process(ChannelAudio(1, CHANNELS_HZ[1], block, 1e-8), t)
            self.assertIsNone(recorder.active)
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
