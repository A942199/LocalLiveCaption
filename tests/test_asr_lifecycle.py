"""ASR-only regression tests: no GPU, network, audio device or translator required."""
import os
import runpy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_regression")
RUN_ASR = MOD["run_asr"]


def options(**changes):
    base = dict(
        device=None, hotwords_file=None, hotwords=None, llama_server="fake",
        model="fake-model", mmproj="fake-mmproj", lang="ja", context=False,
        llama_port=8765, llama_url=None, slots=2, ctx_size=4096,
        context_history=1, context_chars=80, max_tokens=200,
        temperature=0.0, debug=None, stable_count=3, vad_threshold=0.4,
        vad_min_speech_ms=100, vad_min_silence_ms=200, vad_speech_pad_ms=100,
        vad_win=0.2, duration=0.05, max_gain=5, silence=0.5,
        punct_silence=0.4, hesitation_silence=1.1, min_seg=0.2,
        max_seg=10.0, no_partial=True, partial_every=0.5
    )
    base.update(changes)
    return SimpleNamespace(**base)


class FakeBackend:
    last = None

    def __init__(self, *args):
        self.closed = False
        FakeBackend.last = self

    def warmup(self):
        pass

    def transcribe(self, audio):
        return "テスト"

    def close(self):
        self.closed = True


class FakePA:
    last = None

    def __init__(self):
        self.closed = False
        FakePA.last = self

    def terminate(self):
        self.closed = True


class ASRLifecycleTests(unittest.TestCase):
    def test_capture_open_failure_releases_backend_and_pyaudio(self):
        global_objects = RUN_ASR.__globals__
        fake_audio = SimpleNamespace(PyAudio=FakePA)
        fake_device = {"name": "test", "defaultSampleRate": 16000, "maxInputChannels": 1}

        def fail_capture(*args):
            raise OSError("synthetic WASAPI failure")

        with patch.dict(global_objects, {
            "pyaudio": fake_audio, "get_loopback_device": lambda *args: fake_device,
            "LlamaBackend": FakeBackend, "start_capture": fail_capture,
        }), patch(
            "faster_whisper.vad.get_vad_model",
            return_value=SimpleNamespace(session=SimpleNamespace(run=lambda *args: None)),
        ):
            with self.assertRaisesRegex(OSError, "WASAPI"):
                RUN_ASR(options(), None, threading.Event())

        self.assertTrue(FakeBackend.last.closed, "The launched model server must be closed.")
        self.assertTrue(FakePA.last.closed, "PyAudio must be released.")

    def test_vad_init_failure_releases_backend_and_pyaudio(self):
        fake_device = {"name": "test", "defaultSampleRate": 16000, "maxInputChannels": 1}
        with patch.dict(RUN_ASR.__globals__, {
            "pyaudio": SimpleNamespace(PyAudio=FakePA),
            "get_loopback_device": lambda *args: fake_device,
            "LlamaBackend": FakeBackend,
        }), patch("faster_whisper.vad.get_vad_model", side_effect=RuntimeError("bad VAD model")):
            with self.assertRaisesRegex(RuntimeError, "bad VAD"):
                RUN_ASR(options(), None, threading.Event())
        self.assertTrue(FakeBackend.last.closed)
        self.assertTrue(FakePA.last.closed)

    def test_tail_inference_error_still_closes_asr_resources(self):
        import numpy as np

        class ErrorBackend(FakeBackend):
            def transcribe(self, audio):
                raise OSError("synthetic final inference failure")

        class Stream:
            closed = False
            def stop_stream(self):
                pass
            def close(self):
                self.closed = True

        stream = Stream()
        def provide_audio(_, __, audio_q, ___):
            for _ in range(5):
                audio_q.put(np.ones(3200, dtype=np.float32) * 0.1)
            return stream

        class VadSession:
            def run(self, _, inputs):
                return [np.array([0.9], dtype=np.float32), inputs["h"], inputs["c"]]

        fake_device = {"name": "test", "defaultSampleRate": 16000, "maxInputChannels": 1}
        sink = SimpleNamespace(final=lambda *a: None)
        with patch.dict(RUN_ASR.__globals__, {
            "pyaudio": SimpleNamespace(PyAudio=FakePA),
            "get_loopback_device": lambda *args: fake_device,
            "LlamaBackend": ErrorBackend, "start_capture": provide_audio,
        }), patch(
            "faster_whisper.vad.get_vad_model",
            return_value=SimpleNamespace(session=VadSession()),
        ):
            RUN_ASR(options(duration=0.06), sink, threading.Event())
        self.assertTrue(FakeBackend.last.closed)
        self.assertTrue(FakePA.last.closed)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
