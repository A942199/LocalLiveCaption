"""Fresh regressions for third ASR review. Red on former implementation; green on fix."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_third_review_regression")
EDGES = runpy.run_path("tests/test_asr_edge_cases.py", run_name="edge_helpers")
RUN = EDGES["run_injected"]
SPEECH = np.ones(16000, dtype=np.float32) * .2
SILENCE = np.zeros(19200, dtype=np.float32)


class ASRThirdReviewTests(unittest.TestCase):
    def test_final_sink_write_failure_does_not_kill_later_finals(self):
        class Sink:
            def __init__(self):
                self.calls = 0
                self.received = []
            def final(self, text, *_):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("simulated SRT write failure")
                self.received.append(text)
            def partial(self, *_):
                pass
        with patch.dict(RUN.__globals__, {"Sink": Sink}):
            sink, _ = RUN([np.concatenate([SPEECH, SILENCE, SPEECH, SILENCE])], stop_immediately=True)
        self.assertEqual(2, sink.calls)
        self.assertEqual(1, len(sink.received))

    def test_debug_io_error_does_not_kill_later_final(self):
        class BrokenDebug:
            def __init__(self, path):
                self.t0 = time.time()
                self.f = self
            def write(self, *_):
                raise OSError("simulated full debug disk")
            def close(self):
                pass
        with patch.dict(RUN.__globals__, {"args": lambda: EDGES["args"](debug="not-opened")}), patch.dict(
            RUN.__globals__["GLOBALS"], {"DebugLog": BrokenDebug}
        ):
            sink, _ = RUN([np.concatenate([SPEECH, SILENCE, SPEECH, SILENCE])], stop_immediately=True)
        self.assertEqual(2, len(sink.finals))

    def test_one_broken_output_does_not_block_other_caption_sinks(self):
        seen = []
        class Broken:
            def final(self, *_):
                raise OSError("console/SRT output unavailable")
        class Working:
            def final(self, text, *_):
                seen.append(text)
        combined = MOD["MultiSink"](Broken(), Working())
        combined.final("第二个输出仍需显示", 1.0, 2.0)
        self.assertEqual(["第二个输出仍需显示"], seen)

    def test_partial_output_failure_does_not_block_other_sinks(self):
        seen = []
        class Broken:
            def partial(self, *_):
                raise OSError("console unavailable")
        class Working:
            def partial(self, text):
                seen.append(text)
        combined = MOD["MultiSink"](Broken(), Working())
        combined.partial("途中の字幕")
        self.assertEqual(["途中の字幕"], seen)

    def test_two_debug_sessions_do_not_overwrite_wav(self):
        with tempfile.TemporaryDirectory(prefix="asr_debug_keep_") as directory:
            def write(value):
                debug = MOD["DebugLog"](directory)
                row = {"seg": 0, "kind": "final", "reason": "silence"}
                debug.write(row, np.full(16000, value, dtype=np.float32))
                debug.f.close()
                return row["wav"]
            wav1 = write(.1)
            original = (Path(directory) / wav1).read_bytes()
            wav2 = write(.5)
            self.assertNotEqual(wav1, wav2)
            self.assertEqual(original, (Path(directory) / wav1).read_bytes())
            self.assertTrue((Path(directory) / wav2).is_file())

    def test_final_tail_timestamp_excludes_inference_delay(self):
        class SlowBackend(EDGES["FakeBackend"]):
            def transcribe(self, audio):
                time.sleep(.4)
                return "最後です。"
        class TimeSink(EDGES["Sink"]):
            def final(self, text, a, b):
                self.finals.append((text, b - a))
        with patch.dict(RUN.__globals__, {"FakeBackend": SlowBackend, "Sink": TimeSink}):
            sink, _ = RUN([SPEECH], stop_immediately=True)
        self.assertEqual(1, len(sink.finals))
        self.assertLess(sink.finals[0][1], 1.20)  # 1 s voice, not 1.4 s.

    def test_vad_win_zero_is_rejected_with_clear_error(self):
        with patch.dict(RUN.__globals__, {"args": lambda: EDGES["args"](vad_win=0)}):
            with self.assertRaisesRegex(ValueError, "vad.win"):
                RUN([SPEECH], stop_immediately=True)

    def test_reused_owner_disappears_borrower_recovers_without_killing_owner(self):
        Backend = MOD["LlamaBackend"]
        servers = {}
        spawned = []
        class FakeProc:
            returncode = None
            def __init__(self, port):
                self.port = port
                self.dead = False
            def poll(self):
                return 0 if self.dead else None
            def terminate(self):
                self.dead = True
            def kill(self):
                self.dead = True
            def wait(self, timeout=None):
                return 0
        def launch(cmd, **kw):
            port = int(cmd[cmd.index("--port") + 1])
            self.assertNotIn(port, servers)
            proc = FakeProc(port)
            servers[port] = proc
            spawned.append(proc)
            return proc
        class Response:
            status = 200
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self):
                return json.dumps(self.body).encode()
        def urlopen(req, timeout=2):
            url = req if isinstance(req, str) else req.full_url
            from urllib.parse import urlparse
            port = urlparse(url).port
            proc = servers.get(port)
            if proc is None or proc.dead:
                raise urllib.error.URLError("server owner exited")
            if url.endswith("/health"):
                return Response({})
            if url.endswith("/v1/chat/completions"):
                return Response({"choices": [{"message": {"content": "language Japanese<asr_text>大丈夫"}}]})
            raise AssertionError(url)
        # The first backend starts its own server; the second probes and borrows.
        def probe(_, url, model):
            from urllib.parse import urlparse
            port = urlparse(url).port
            proc = servers.get(port)
            return "same" if proc and not proc.dead else "none"
        init = dict(server_exe="fake-server.exe", model="model.gguf", mmproj="audio.gguf",
                    lang="ja", hotwords="", use_context=False, port=8765)
        with patch.object(Backend, "_probe", probe), patch.object(Backend, "_bind_job", return_value=None), patch(
            "subprocess.Popen", side_effect=launch
        ), patch.object(urllib.request, "urlopen", side_effect=urlopen):
            owner = Backend(**init)
            borrower = Backend(**init)
            try:
                self.assertEqual("大丈夫", borrower.transcribe(SPEECH))
                owner.close()
                self.assertEqual("大丈夫", borrower.transcribe(SPEECH))
                self.assertGreaterEqual(len(spawned), 2)
                self.assertNotEqual(spawned[0].port, spawned[1].port)
                self.assertTrue(spawned[0].dead)
            finally:
                borrower.close()


if __name__ == "__main__":
    unittest.main()
