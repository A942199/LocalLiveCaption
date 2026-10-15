"""ASR boundary regressions: large callbacks, shutdown, WASAPI, SRT, llama reuse.

Isolated from GPU, Windows audio, network and translation.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_edge_regressions")
GLOBALS = MOD["run_asr"].__globals__


class FakeBackend:
    last = None

    def __init__(self, *args):
        self.calls = []
        self.closed = False
        FakeBackend.last = self

    def warmup(self):
        pass

    def transcribe(self, audio):
        self.calls.append(len(audio))
        return "最後の言葉です。"

    def add_context(self, text):
        pass

    def close(self):
        self.closed = True


class FakeVAD:
    def run(self, outputs, inputs):
        speech = np.max(np.abs(inputs["input"])) > 0.02
        return [np.array([0.99 if speech else 0.01], dtype=np.float32),
                inputs["h"], inputs["c"]]


class FakeStream:
    def __init__(self):
        self.closed = False
        self.stopped = False

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeAudio:
    last = None

    def __init__(self):
        self.closed = False
        FakeAudio.last = self

    def terminate(self):
        self.closed = True


def args(**overrides):
    values = dict(
        device=None, hotwords_file=None, hotwords=None, llama_server="fake",
        model="fake", mmproj="fake", lang="ja", context=False,
        llama_port=8765, llama_url=None, slots=2, ctx_size=4096,
        context_history=1, context_chars=80, max_tokens=200, temperature=0,
        debug=None, stable_count=3, vad_threshold=.4, vad_min_speech_ms=100,
        vad_min_silence_ms=200, vad_speech_pad_ms=100, vad_win=.2,
        duration=None, max_gain=5, silence=.5, punct_silence=.4,
        hesitation_silence=1.1, min_seg=.5, max_seg=6,
        no_partial=True, partial_every=.5,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Sink:
    def __init__(self):
        self.finals = []

    def final(self, text, *args):
        self.finals.append(text)

    def partial(self, text):
        pass


def run_injected(chunks, stop_immediately=False):
    sink = Sink()
    stop = threading.Event()
    stream = FakeStream()

    def capture(pa, dev, audio_q, stop_event):
        for chunk in chunks:
            audio_q.put(chunk)
        if stop_immediately:
            stop_event.set()
        else:
            def delayed_stop():
                time.sleep(.2)
                stop_event.set()
            threading.Thread(target=delayed_stop, daemon=True).start()
        return stream

    with patch.dict(GLOBALS, {
        "pyaudio": SimpleNamespace(PyAudio=FakeAudio),
        "get_loopback_device": lambda *a: {
            "name": "synthetic", "defaultSampleRate": 16000, "maxInputChannels": 1,
        },
        "LlamaBackend": FakeBackend,
        "start_capture": capture,
    }), patch("faster_whisper.vad.get_vad_model",
              return_value=SimpleNamespace(session=FakeVAD())):
        MOD["run_asr"](args(), sink, stop)
    return sink, stream


class ASREdgeRegressionTests(unittest.TestCase):
    def test_one_large_audio_chunk_consumes_all_samples_before_exit(self):
        sink, stream = run_injected([np.ones(16000, dtype=np.float32) * .2])
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertGreaterEqual(FakeBackend.last.calls[-1], 14400)
        self.assertTrue(stream.closed)

    def test_stop_drains_already_queued_audio(self):
        speech = np.ones(3200, dtype=np.float32) * .2
        sink, _ = run_injected([speech.copy() for _ in range(5)], stop_immediately=True)
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertGreaterEqual(FakeBackend.last.calls[-1], 14400)

    def test_failed_stream_start_closes_opened_stream(self):
        class Broken(FakeStream):
            def start_stream(self):
                raise OSError("cannot start WASAPI")

        class PA:
            def __init__(self):
                self.stream = Broken()

            def open(self, **kwargs):
                return self.stream

        audio = PA()
        capture_globals = MOD["start_capture"].__globals__
        with patch.dict(capture_globals, {
            "pyaudio": SimpleNamespace(paFloat32=1, paComplete=2, paContinue=3),
        }):
            with self.assertRaisesRegex(OSError, "cannot start WASAPI"):
                MOD["start_capture"](
                    audio, {"maxInputChannels": 1, "defaultSampleRate": 16000,
                            "index": 0},
                    MOD["_AudioChunkQueue"](), threading.Event()
                )
        self.assertTrue(audio.stream.closed)

    def test_srt_append_reuses_next_number(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "captions.srt")
            with contextlib.redirect_stdout(io.StringIO()):
                first = MOD["ConsoleSink"](srt_path=path)
                first.final("一つ", first.t0, first.t0 + 1)
                first.close()
                second = MOD["ConsoleSink"](srt_path=path)
                second.final("二つ", second.t0, second.t0 + 1)
                second.close()
            self.assertEqual(["1", "2"], [
                x.strip() for x in Path(path).read_text(encoding="utf-8").splitlines()
                if x.strip().isdigit()
            ])

    def test_srt_timestamp_supports_over_24_hours(self):
        self.assertEqual("24:00:01,500", MOD["fmt_ts"](86401.5))
        self.assertEqual("49:01:02,003", MOD["fmt_ts"](176462.003))

    def test_different_path_same_model_basename_is_not_reused(self):
        be = MOD["LlamaBackend"].__new__(MOD["LlamaBackend"])
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({
                    "model_path": r"C:\another-folder\Qwen3-ASR-1.7B-Q8_0.gguf",
                }).encode()
        be.urllib = SimpleNamespace(urlopen=lambda *a, **k: Response())
        with patch.object(socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 0
            result = be._probe("http://127.0.0.1:8765", r"C:\my-folder\Qwen3-ASR-1.7B-Q8_0.gguf")
        self.assertEqual("other", result)

    def test_stop_drains_multiple_queued_final_segments_in_order(self):
        spoken = np.ones(16000, dtype=np.float32) * .2
        quiet = np.zeros(19200, dtype=np.float32)
        # A single oversized callback contains two utterances with a pause.
        signal = np.concatenate((spoken, quiet, spoken, quiet))
        sink, _ = run_injected([signal], stop_immediately=True)
        self.assertEqual(["最後の言葉です。", "最後の言葉です。"], sink.finals)
        self.assertEqual(2, len(FakeBackend.last.calls))
        self.assertTrue(FakeBackend.last.closed)

    def test_shutdown_preserves_subwindow_audio_tail(self):
        signal = np.ones(15200, dtype=np.float32) * .2
        sink, _ = run_injected([signal], stop_immediately=True)
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertEqual(15200, FakeBackend.last.calls[-1])

    def test_srt_append_continues_timing_after_existing_cue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "captions.srt")
            with contextlib.redirect_stdout(io.StringIO()):
                first = MOD["ConsoleSink"](srt_path=path)
                first.final("第一段", first.t0, first.t0 + 2.25)
                first.close()
                second = MOD["ConsoleSink"](srt_path=path)
                second.final("第二段", second.t0, second.t0 + 1.0)
                second.close()
            cues = Path(path).read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:02,250", cues)
            self.assertIn("00:00:02,250 --> 00:00:03,250", cues)

    def test_capture_init_failure_leaves_no_asr_worker_thread(self):
        baseline = {t.ident for t in threading.enumerate()}
        def fail_capture(*args):
            raise OSError("simulated WASAPI open failure")
        stop = threading.Event()
        with patch.dict(GLOBALS, {
            "pyaudio": SimpleNamespace(PyAudio=FakeAudio),
            "get_loopback_device": lambda *a: {
                "name": "synthetic", "defaultSampleRate": 16000,
                "maxInputChannels": 1,
            },
            "LlamaBackend": FakeBackend, "start_capture": fail_capture,
        }), patch("faster_whisper.vad.get_vad_model",
                  return_value=SimpleNamespace(session=FakeVAD())):
            with self.assertRaisesRegex(OSError, "WASAPI"):
                MOD["run_asr"](args(), Sink(), stop)
        time.sleep(.25)
        leaking = [t.name for t in threading.enumerate() if t.ident not in baseline]
        self.assertEqual([], leaking, "Workers must not start before capture is ready")


if __name__ == "__main__":
    unittest.main()
