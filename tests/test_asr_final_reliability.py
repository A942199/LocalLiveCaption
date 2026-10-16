"""ASR final reliability regressions; no GPU, audio device, or network."""
import json
import os
import runpy
import threading
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
EDGES = runpy.run_path("tests/test_asr_edge_cases.py", run_name="asr_fix_edge_helpers")
MOD = EDGES["MOD"]
SR = MOD["SR"]


class Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self):
        return json.dumps(self.body).encode()


def make_backend():
    be = MOD["LlamaBackend"].__new__(MOD["LlamaBackend"])
    be.lang = "Japanese"
    be.hot = ""
    be.history = []
    be.use_context = False
    be.max_tokens = 256
    be.temperature = 0.0
    be.url = "http://127.0.0.1:8765"
    be._server_generation = 0
    return be


class ASRFinalReliabilityTests(unittest.TestCase):
    def test_shutdown_preserves_short_valid_speech_below_half_second(self):
        audio = np.ones(int(.4 * SR), dtype=np.float32) * .2
        sink, _ = EDGES["run_injected"]([audio], stop_immediately=True)
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertEqual(len(audio), EDGES["FakeBackend"].last.calls[-1])

    def test_truncated_generation_retries_with_larger_token_budget(self):
        be = make_backend()
        budgets = []
        def urlopen(req, timeout=60):
            body = json.loads(req.data)
            budgets.append(body["max_tokens"])
            if len(budgets) == 1:
                return Response({"choices":[{"message":{"content":"language Japanese<asr_text>まだ"},"finish_reason":"length"}]})
            return Response({"choices":[{"message":{"content":"language Japanese<asr_text>まだ続きます。"},"finish_reason":"stop"}]})
        be.urllib = SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)
        self.assertEqual("まだ続きます。", be.transcribe(np.zeros(1600, dtype=np.float32)))
        self.assertEqual([256, 512], budgets)

    def test_repeated_truncation_never_returns_incomplete_final(self):
        be = make_backend()
        def urlopen(req, timeout=60):
            return Response({"choices":[{"message":{"content":"language Japanese<asr_text>中途半端"},"finish_reason":"length"}]})
        be.urllib = SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)
        with self.assertRaisesRegex(Exception, "truncat|截断|length"):
            be.transcribe(np.zeros(1600, dtype=np.float32))

    def test_final_worker_retries_one_transient_backend_failure(self):
        class OnceFail(EDGES["FakeBackend"]):
            calls_count = 0
            def transcribe(self, audio):
                type(self).calls_count += 1
                if type(self).calls_count == 1:
                    raise OSError("temporary server reset")
                return super().transcribe(audio)
        with patch.dict(EDGES["run_injected"].__globals__, {"FakeBackend": OnceFail}):
            sink, _ = EDGES["run_injected"](
                [np.ones(SR, dtype=np.float32) * .2], stop_immediately=True
            )
        self.assertEqual(["最後の言葉です。"], sink.finals)
        self.assertEqual(2, OnceFail.calls_count)


if __name__ == "__main__":
    unittest.main()
