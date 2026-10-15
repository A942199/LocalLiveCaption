"""Streaming VAD and bounded audio regression tests (no audio device needed)."""
import os
import runpy
import unittest
import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_streaming_regression")


class Session:
    def __init__(self):
        self.incoming_states = []

    def run(self, _, inputs):
        self.incoming_states.append(float(inputs["h"].flat[0]))
        next_h = inputs["h"].copy()
        next_h += 1
        next_c = inputs["c"].copy()
        next_c += 1
        return [np.array([0.90], dtype=np.float32), next_h, next_c]


class StreamingTests(unittest.TestCase):
    def test_vad_preserves_model_state_across_separate_windows(self):
        s = Session()
        model = type("FakeModel", (), {"session": s})()
        vad = MOD["_StreamingSileroVAD"](model, threshold=0.4, min_speech_ms=64, min_silence_ms=100)
        vad(np.ones(512, dtype=np.float32))
        self.assertTrue(vad(np.ones(512, dtype=np.float32)))
        self.assertEqual([0.0, 1.0], s.incoming_states)
        vad.reset()
        vad(np.ones(512, dtype=np.float32))
        self.assertEqual(0.0, s.incoming_states[-1])

    def test_audio_capture_queue_is_bounded_and_indicates_gaps(self):
        q = MOD["_AudioChunkQueue"](max_chunks=2)
        q.put(np.array([1.0], dtype=np.float32))
        q.put(np.array([2.0], dtype=np.float32))
        q.put(np.array([3.0], dtype=np.float32))
        self.assertEqual(1, q.dropped)
        first_audio, _, first_seq = q.get(timeout=0.1)
        second_audio, _, second_seq = q.get(timeout=0.1)
        self.assertEqual((1, 2), (first_seq, second_seq))
        self.assertEqual((2.0, 3.0), (float(first_audio[0]), float(second_audio[0])))


if __name__ == "__main__":
    unittest.main()
