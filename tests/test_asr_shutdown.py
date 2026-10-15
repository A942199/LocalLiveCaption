"""Regression for ASR shutdown after an in-flight Final result.

No GPU, network, audio device or translation service required.
The slow test reproduces the original 10-second join race on Windows.
"""
import os
import runpy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_shutdown_regression")
RUN_ASR = MOD["run_asr"]


class _FakeAudio:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


class _FakeStream:
    def __init__(self):
        self.closed = False

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _FakeSileroSession:
    def run(self, outputs, inputs):
        speech = float(np.max(np.abs(inputs["input"]))) > 0.02
        return [
            np.array([0.99 if speech else 0.01], dtype=np.float32),
            inputs["h"],
            inputs["c"],
        ]


class ASRShutdownRegressionTests(unittest.TestCase):
    def test_slow_final_is_emitted_before_backend_closes_and_run_asr_returns(self):
        stop = threading.Event()
        completed = threading.Event()
        stream = _FakeStream()
        fake_audio = _FakeAudio()
        events = []
        fake_backend = None

        class SlowBackend:
            def __init__(self, *args):
                nonlocal fake_backend
                self.closed = False
                fake_backend = self

            def warmup(self):
                pass

            def transcribe(self, audio):
                # Previously the Final thread was only joined for 10 seconds.
                time.sleep(11.2)
                completed.set()
                return "最後の一文です。"

            def add_context(self, text):
                pass

            def close(self):
                self.closed = True

        class Sink:
            def final(self, text, *times):
                events.append((text, fake_backend.closed))

            def partial(self, text):
                pass

        def capture(pa, dev, audio_q, stop_event):
            for _ in range(5):
                audio_q.put(np.full(3200, 0.2, dtype=np.float32))
            for _ in range(10):
                audio_q.put(np.zeros(3200, dtype=np.float32))

            def request_shutdown():
                # Wait until Final is submitted and its worker has started.
                # With only silence left, this is enough to avoid any tail re-ASR.
                time.sleep(0.3)
                stop_event.set()

            threading.Thread(target=request_shutdown, daemon=True).start()
            return stream

        args = SimpleNamespace(
            device=None, hotwords_file=None, hotwords=None, llama_server="fake",
            model="fake", mmproj="fake", lang="ja", context=False,
            llama_port=8765, llama_url=None, slots=2, ctx_size=4096,
            context_history=1, context_chars=80, max_tokens=200,
            temperature=0, debug=None, stable_count=3, vad_threshold=0.4,
            vad_min_speech_ms=100, vad_min_silence_ms=200, vad_speech_pad_ms=100,
            vad_win=0.2, duration=None, max_gain=5, silence=0.5,
            punct_silence=0.4, hesitation_silence=1.1,
            min_seg=0.5, max_seg=6, no_partial=True, partial_every=0.5,
        )
        with patch.dict(RUN_ASR.__globals__, {
            "pyaudio": SimpleNamespace(PyAudio=lambda: fake_audio),
            "get_loopback_device": lambda *a: {
                "name": "synthetic", "defaultSampleRate": 16000, "maxInputChannels": 1,
            },
            "LlamaBackend": SlowBackend,
            "start_capture": capture,
        }), patch(
            "faster_whisper.vad.get_vad_model",
            return_value=SimpleNamespace(session=_FakeSileroSession()),
        ):
            started = time.perf_counter()
            RUN_ASR(args, Sink(), stop)
            elapsed = time.perf_counter() - started
            returned_before_completion = not completed.is_set()
            events_at_return = list(events)
            # If the old buggy implementation returns early, do not leave a
            # daemon ASR thread running after unittest completes.
            completed.wait(timeout=3)

        self.assertFalse(returned_before_completion, "ASR returned before Final inference completed")
        self.assertEqual(
            [("最後の一文です。", False)], events_at_return,
            "Final must arrive once before the backend is closed and run_asr returns",
        )
        self.assertTrue(fake_backend.closed)
        self.assertTrue(fake_audio.terminated)
        self.assertTrue(stream.closed)
        self.assertGreater(elapsed, 10.5)

    def test_queued_final_is_emitted_before_unfinished_tail(self):
        stop = threading.Event()
        first_inference_started = threading.Event()
        stream = _FakeStream()
        fake_audio = _FakeAudio()
        outputs = []
        backend = None

        class OrderedBackend:
            def __init__(self, *args):
                nonlocal backend
                self.closed = False
                backend = self

            def warmup(self):
                pass

            def transcribe(self, audio):
                if len(audio) > 16000 * 1.3:
                    first_inference_started.set()
                    time.sleep(0.45)
                    return "先说的句子。"
                return "退出前的最后一句。"

            def add_context(self, text):
                pass

            def close(self):
                self.closed = True

        def capture(pa, dev, audio_q, stop_event):
            for _ in range(5):
                audio_q.put(np.full(3200, 0.2, dtype=np.float32))
            for _ in range(7):
                audio_q.put(np.zeros(3200, dtype=np.float32))
            for _ in range(5):
                audio_q.put(np.full(3200, 0.2, dtype=np.float32))

            def close_during_first_inference():
                if first_inference_started.wait(timeout=3):
                    time.sleep(0.1)
                stop_event.set()

            threading.Thread(target=close_during_first_inference, daemon=True).start()
            return stream

        args = SimpleNamespace(
            device=None, hotwords_file=None, hotwords=None, llama_server="fake",
            model="fake", mmproj="fake", lang="ja", context=False,
            llama_port=8765, llama_url=None, slots=2, ctx_size=4096,
            context_history=1, context_chars=80, max_tokens=200,
            temperature=0, debug=None, stable_count=3, vad_threshold=0.4,
            vad_min_speech_ms=100, vad_min_silence_ms=200, vad_speech_pad_ms=100,
            vad_win=0.2, duration=None, max_gain=5, silence=0.5,
            punct_silence=0.4, hesitation_silence=1.1,
            min_seg=0.5, max_seg=6, no_partial=True, partial_every=0.5,
        )
        sink = SimpleNamespace(
            final=lambda text, *_: outputs.append((text, backend.closed)),
            partial=lambda text: None,
        )
        with patch.dict(RUN_ASR.__globals__, {
            "pyaudio": SimpleNamespace(PyAudio=lambda: fake_audio),
            "get_loopback_device": lambda *a: {
                "name": "synthetic", "defaultSampleRate": 16000, "maxInputChannels": 1,
            },
            "LlamaBackend": OrderedBackend, "start_capture": capture,
        }), patch(
            "faster_whisper.vad.get_vad_model",
            return_value=SimpleNamespace(session=_FakeSileroSession()),
        ):
            RUN_ASR(args, sink, stop)

        self.assertTrue(first_inference_started.is_set())
        self.assertEqual([
            ("先说的句子。", False),
            ("退出前的最后一句。", False),
        ], outputs, "Drained queued Final must precede the unfinished tail.")
        self.assertTrue(backend.closed)
        self.assertTrue(fake_audio.terminated)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
