"""Fourth-review regressions for capture failure, remote mode, and Final tail.

Uses stubbed audio/server, no hardware or network, no project artifacts.
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
EDGE = runpy.run_path("tests/test_asr_edge_cases.py", run_name="fourth_edge_import")
MOD = EDGE["MOD"]
GLOBAL = MOD["run_asr"].__globals__
SR = 16000


def setup_env(capture, backend=None, debug=None, finder=None):
    overrides = {
        "pyaudio": SimpleNamespace(PyAudio=EDGE["FakeAudio"]),
        "get_loopback_device": lambda *a: {
            "name": "injected loopback", "defaultSampleRate": SR,
            "maxInputChannels": 1,
        },
        "LlamaBackend": backend or EDGE["FakeBackend"],
        "start_capture": capture,
    }
    if debug:
        overrides["DebugLog"] = debug
    if finder:
        overrides["find_llama_server"] = finder
    return patch.dict(GLOBAL, overrides)


class FourthASRReviewTests(unittest.TestCase):
    def test_dead_device_reports_failure_and_exits_without_external_stop(self):
        class StoppedStream(EDGE["FakeStream"]):
            def __init__(self):
                super().__init__()
                self.polled = 0
            def is_active(self):
                self.polled += 1
                return False
        stream = StoppedStream()
        statuses = []
        stop = threading.Event()
        def capture(pa, dev, audio_q, stop_event):
            return stream
        watchdog = threading.Timer(1.5, stop.set)
        watchdog.daemon = True
        watchdog.start()
        try:
            with setup_env(capture), patch("faster_whisper.vad.get_vad_model",
                     return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
                start = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "audio|音频|WASAPI|采集"):
                    MOD["run_asr"](EDGE["args"](), EDGE["Sink"](), stop,
                                   status=statuses.append)
                self.assertLess(time.monotonic() - start, 1.4)
        finally:
            stop.set()
            watchdog.cancel()
        self.assertGreater(stream.polled, 0)
        self.assertTrue(stream.closed)
        self.assertTrue(EDGE["FakeAudio"].last.closed)
        self.assertTrue(any(("失败" in s or "中断" in s or "异常" in s) for s in statuses))

    def test_active_stream_with_no_audio_does_not_report_failure(self):
        class HealthyStream(EDGE["FakeStream"]):
            def is_active(self):
                return True
        stream = HealthyStream()
        stop = threading.Event()
        statuses = []
        def capture(pa, dev, audio_q, stop_event):
            threading.Thread(target=lambda: (time.sleep(.75), stop_event.set()),
                             daemon=True).start()
            return stream
        with setup_env(capture), patch("faster_whisper.vad.get_vad_model",
                 return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
            MOD["run_asr"](EDGE["args"](), EDGE["Sink"](), stop,
                           status=statuses.append)
        self.assertTrue(stream.closed)
        self.assertFalse(any("中断" in s or "失败" in s for s in statuses))

    def test_disconnected_device_flushes_captured_speech_before_error(self):
        class StoppedStream(EDGE["FakeStream"]):
            def is_active(self):
                return False
        stream = StoppedStream()
        stop = threading.Event()
        sink = EDGE["Sink"]()
        def capture(pa, dev, q, event):
            q.put(np.ones(16000, dtype=np.float32) * .2)
            return stream
        with setup_env(capture), patch("faster_whisper.vad.get_vad_model",
                 return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
            with self.assertRaisesRegex(RuntimeError, "采集"):
                MOD["run_asr"](EDGE["args"](), sink, stop)
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertTrue(stream.closed)
        self.assertTrue(EDGE["FakeBackend"].last.closed)

    def test_unreadable_device_state_reports_failure_and_cleans_up(self):
        class BrokenStream(EDGE["FakeStream"]):
            def is_active(self):
                raise OSError("WASAPI audio engine lost")
        stream = BrokenStream()
        stop = threading.Event()
        statuses = []
        def capture(pa, dev, q, event):
            return stream
        with setup_env(capture), patch("faster_whisper.vad.get_vad_model",
                 return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
            with self.assertRaisesRegex(RuntimeError, "状态查询失败"):
                MOD["run_asr"](EDGE["args"](), EDGE["Sink"](), stop,
                               status=statuses.append)
        self.assertTrue(stream.closed)
        self.assertTrue(any("中断" in x for x in statuses))

    def test_shutdown_silent_tail_does_not_extend_end_of_speech(self):
        class TimeSink(EDGE["Sink"]):
            def __init__(self):
                super().__init__()
                self.intervals = []
            def final(self, text, start, end):
                self.finals.append(text)
                self.intervals.append((start, end))
        stop = threading.Event()
        sink = TimeSink()
        stream = EDGE["FakeStream"]()
        signal = np.concatenate((
            np.ones(12800, dtype=np.float32) * .2,
            np.zeros(2400, dtype=np.float32),
        ))
        def capture(pa, dev, q, event):
            q.put(signal)
            event.set()
            return stream
        with setup_env(capture), patch("faster_whisper.vad.get_vad_model",
                 return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
            MOD["run_asr"](EDGE["args"](), sink, stop)
        self.assertEqual(1, len(sink.intervals))
        start, end = sink.intervals[0]
        self.assertAlmostEqual(.8, end - start, delta=.025)

    def test_remote_url_does_not_resolve_local_server_binary(self):
        found = []
        passed = []
        class RemoteBackend(EDGE["FakeBackend"]):
            def __init__(self, server_exe, *args):
                passed.append((server_exe, args[6]))  # url arg in constructor
                super().__init__()
        def missing_exe(*a):
            found.append(True)
            raise RuntimeError("local executable does not exist")
        stream = EDGE["FakeStream"]()
        stop = threading.Event()
        def capture(pa, dev, q, event):
            event.set()
            return stream
        with setup_env(capture, backend=RemoteBackend, finder=missing_exe), patch(
                "faster_whisper.vad.get_vad_model",
                return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
            MOD["run_asr"](EDGE["args"](llama_url="http://127.0.0.1:9998",
                                         llama_server=None), EDGE["Sink"](), stop)
        self.assertEqual([], found)
        self.assertEqual("http://127.0.0.1:9998", passed[0][1])
        self.assertIsNone(passed[0][0])

    def test_shutdown_tail_subwindow_has_correct_end_time_and_debug_record(self):
        class TimeSink(EDGE["Sink"]):
            def __init__(self):
                super().__init__()
                self.intervals = []
            def final(self, text, a, b):
                self.finals.append(text)
                self.intervals.append((a, b))
        class Debug:
            instances = []
            def __init__(self, d):
                self.t0 = time.time()
                self.rows = []
                self.f = self
                self.closed = False
                self.instances.append(self)
            def write(self, record, audio=None):
                self.rows.append((dict(record), len(audio) if audio is not None else 0))
            def close(self):
                self.closed = True
        for seconds in (.8, .95, 1.0):
            with self.subTest(seconds=seconds):
                n = int(seconds * SR)
                signal = np.ones(n, dtype=np.float32) * .2
                stop = threading.Event()
                stream = EDGE["FakeStream"]()
                sink = TimeSink()
                def capture(pa, dev, q, event):
                    q.put(signal)
                    event.set()
                    return stream
                with setup_env(capture, debug=Debug), patch(
                    "faster_whisper.vad.get_vad_model",
                    return_value=SimpleNamespace(session=EDGE["FakeVAD"]())):
                    MOD["run_asr"](EDGE["args"](debug="injected-debug-folder"),
                                   sink, stop)
                self.assertEqual(1, len(sink.intervals))
                start, end = sink.intervals[0]
                self.assertAlmostEqual(seconds, end-start, delta=.025)
                dbg = Debug.instances[-1]
                self.assertEqual(1, len(dbg.rows))
                rec, wav_samples = dbg.rows[0]
                self.assertEqual("final", rec["kind"])
                self.assertEqual(n, wav_samples)
                self.assertTrue(dbg.closed)


if __name__ == "__main__":
    unittest.main()
