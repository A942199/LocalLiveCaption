"""Accuracy parameter regression tests for the local Qwen3-ASR backend."""
import json
import os
import runpy
import unittest
import urllib.request
from types import SimpleNamespace

import numpy as np

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="asr_accuracy_tests")


class _Response:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode()


class AccuracyParameterTests(unittest.TestCase):
    def test_forced_japanese_language_is_in_generation_prefix(self):
        seen = {}
        be = MOD["LlamaBackend"].__new__(MOD["LlamaBackend"])
        be.lang = "Japanese"
        be.hot = ""
        be.use_context = False
        be.history = []
        be.context_chars = 80
        be.max_tokens = 256
        be.temperature = 0
        be.url = "http://127.0.0.1:8765"
        be._server_generation = 0

        def urlopen(request, timeout=0):
            seen["request"] = json.loads(request.data.decode())
            return _Response("こんにちは")

        be.urllib = SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)
        self.assertEqual("こんにちは", be.transcribe(np.zeros(16000, dtype=np.float32)))
        self.assertEqual(
            {"role": "assistant", "content": "language Japanese<asr_text>"},
            seen["request"]["messages"][-1],
        )

    def test_low_vad_threshold_stops_after_quiet_frames(self):
        class Session:
            def __init__(self):
                self.i = 0

            def run(self, _, inputs):
                self.i += 1
                prob = .9 if self.i <= 4 else .05
                return np.array([prob], dtype=np.float32), inputs["h"], inputs["c"]

        vad = MOD["_StreamingSileroVAD"](
            SimpleNamespace(session=Session()), threshold=.10,
            min_speech_ms=100, min_silence_ms=100,
        )
        states = [vad(np.zeros(512, dtype=np.float32)) for _ in range(16)]
        self.assertTrue(states[3])
        self.assertFalse(states[-1])
        self.assertFalse(vad.speaking)

    def test_vad_hysteresis_band_eventually_closes_stale_speech(self):
        class Session:
            def __init__(self):
                self.i = 0

            def run(self, _, inputs):
                self.i += 1
                probability = .9 if self.i <= 4 else .08
                return np.array([probability], dtype=np.float32), inputs["h"], inputs["c"]

        vad = MOD["_StreamingSileroVAD"](
            SimpleNamespace(session=Session()), threshold=.10,
            min_speech_ms=100, min_silence_ms=100,
        )
        for _ in range(28):
            vad(np.zeros(512, dtype=np.float32))
        self.assertFalse(vad.speaking)

    def test_padding_is_exact_even_with_200ms_windows(self):
        preroll = MOD["_SpeechPreroll"](int(.25 * MOD["SR"]))
        preroll.add(np.ones(int(.2 * MOD["SR"]), dtype=np.float32))
        preroll.add(np.full(int(.2 * MOD["SR"]), 2, dtype=np.float32))
        audio = preroll.take()
        self.assertEqual(int(.25 * MOD["SR"]), len(audio))
        self.assertEqual(1, audio[0])
        self.assertEqual(2, audio[-1])
        self.assertEqual(0, len(preroll.take()))

    def test_clean_preserves_repeated_spoken_japanese(self):
        self.assertEqual("ははははは", MOD["_clean"]("ははははは"))
        self.assertEqual("はいはいはいはい", MOD["_clean"]("はいはいはいはい"))

    def test_server_probe_rejects_other_parallel_context(self):
        from unittest.mock import patch
        be = MOD["LlamaBackend"].__new__(MOD["LlamaBackend"])
        model = r"C:\\models\\asr.gguf"

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({
                    "model_path": model, "total_slots": 1,
                    "default_generation_settings": {"n_ctx": 4096},
                    "modalities": {"audio": True},
                }).encode()

        be.urllib = SimpleNamespace(urlopen=lambda *_args, **_kwargs: Response())
        with patch("socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 0
            result = be._probe("http://127.0.0.1:8765", model, slots=2, ctx_size=4096)
        self.assertEqual("other", result)

    def test_shared_config_parser_respects_saved_and_explicit_cli(self):
        args = SimpleNamespace(
            model=None, context=True, vad_threshold=.45, max_seg=6.,
            ctx_size=8192, max_tokens=200, slots=2,
        )
        saved = {
            "asr_model": "Q8_0", "context_enabled": False,
            "vad_threshold": .1, "max_seg": 12.,
            "llama_ctx": 4096, "max_tokens": 256,
        }
        MOD["_apply_asr_config"](
            args, saved, lambda opt: opt == "--max-seg", ".", status=None,
        )
        self.assertAlmostEqual(.1, args.vad_threshold)
        self.assertEqual(6., args.max_seg)
        self.assertEqual(4096, args.ctx_size)
        self.assertEqual(256, args.max_tokens)
        self.assertFalse(args.context)


if __name__ == "__main__":
    unittest.main()
