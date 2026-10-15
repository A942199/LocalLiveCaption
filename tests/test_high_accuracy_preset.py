"""Tests for the GUI high-accuracy preset and its persisted Japanese language."""
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="accuracy_preset_tests")
Overlay = MOD["OverlaySink"]


def sample_args():
    return SimpleNamespace(
        model=None, lang=None, context=True, ctx_size=8192, slots=1,
        max_tokens=200, temperature=.2, partial_every=.7,
        vad_speech_pad_ms=100, vad_threshold=.45, max_seg=6.,
        min_seg=1.0,
    )


class HighAccuracyPresetTests(unittest.TestCase):
    @unittest.skipIf(os.environ.get("GITHUB_ACTIONS") == "true",
                     "Real Tk widgets require an interactive Windows desktop")
    def test_real_gui_button_sets_every_preset_and_persists_japanese(self):
        # Never read/write the user's actual live-caption-settings.json.
        with patch.object(Overlay, "_load_settings", autospec=True,
                          side_effect=lambda _self, defaults: dict(defaults)), \
             patch.object(Overlay, "_schedule_save"):
            overlay = Overlay()
            try:
                overlay.root.withdraw()
                self.assertEqual("auto", overlay.asr_language)
                overlay.open_settings()
                overlay.settings_win.withdraw()
                overlay._open_asr_settings()
                overlay.asr_settings_win.withdraw()

                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)

                controls = list(descendants(overlay.asr_settings_win))
                button = next(w for w in controls
                              if w.winfo_class() == "Button" and
                              w.cget("text") == "高精度推荐")
                lang_combo = next(w for w in controls
                                  if w.winfo_class() == "TCombobox" and
                                  str(w.cget("textvariable")) ==
                                  str(overlay._setting_vars["asr_language"]))
                self.assertEqual(("auto", "ja"), tuple(lang_combo.cget("values")))
                button.invoke()  # Actual Tk widget command, not just source inspection.

                expected = {
                    "asr_model": "Q8_0", "asr_language": "ja",
                    "context_enabled": False, "context_history": 1,
                    "context_chars": 80, "llama_ctx": 4096, "slots": 2,
                    "max_tokens": 256, "temperature": 0.0,
                    "partial_every": .4, "stable_count": 3,
                    "punct_silence": .4, "hesitation_silence": 1.1,
                    "silence": .8, "vad_win": .2, "vad_threshold": .1,
                    "vad_min_speech_ms": 100, "vad_min_silence_ms": 100,
                    "vad_speech_pad_ms": 200, "min_seg": 1.2,
                    "max_seg": 16.0, "max_gain": 60.0,
                }
                for key, value in expected.items():
                    with self.subTest(key=key):
                        self.assertEqual(value, overlay._setting_vars[key].get())
                        self.assertEqual(value, overlay._current_settings()[key])
                self.assertEqual("ja", overlay.asr_language)

                with tempfile.TemporaryDirectory(prefix="accuracy_preset_") as td:
                    overlay.settings_path = str(Path(td) / "settings.json")
                    overlay._save_settings()
                    saved = json.loads(Path(overlay.settings_path).read_text(encoding="utf8"))
                    for key, value in expected.items():
                        with self.subTest(persisted=key):
                            self.assertEqual(value, saved[key])
                    args = sample_args()
                    MOD["_apply_asr_config"](args, saved, lambda _opt: False, td)
                    self.assertEqual("ja", args.lang)
                    self.assertFalse(args.context)
                    self.assertEqual(4096, args.ctx_size)
                    self.assertEqual(16., args.max_seg)
                    self.assertEqual(200, args.vad_speech_pad_ms)
                    self.assertEqual(.4, args.partial_every)
                    self.assertIn("Qwen3-ASR-1.7B-Q8_0.gguf", args.model)

                overlay._reset_settings()
                self.assertEqual("auto", overlay.asr_language)
                self.assertEqual("auto", overlay._setting_vars["asr_language"].get())
            finally:
                overlay.root.destroy()

    def test_old_settings_do_not_force_language_and_cli_override_wins(self):
        old = {"asr_model": "Q8_0", "max_seg": 6.}
        args = sample_args()
        MOD["_apply_asr_config"](args, old, lambda _opt: False, ".")
        self.assertIsNone(args.lang)

        saved = dict(old, asr_language="ja", context_enabled=False)
        args = sample_args()
        MOD["_apply_asr_config"](args, saved, lambda opt: opt == "--lang", ".")
        self.assertIsNone(args.lang)  # Explicit --lang auto / no specified value.
        args = sample_args()
        args.lang = "en"
        MOD["_apply_asr_config"](args, saved, lambda opt: opt == "--lang", ".")
        self.assertEqual("en", args.lang)
        args = sample_args()
        MOD["_apply_asr_config"](args, dict(old, asr_language="auto"),
                                 lambda _opt: False, ".")
        self.assertIsNone(args.lang)
        args = sample_args()
        MOD["_apply_asr_config"](args, dict(old, asr_language="unknown"),
                                 lambda _opt: False, ".")
        self.assertIsNone(args.lang)


if __name__ == "__main__":
    unittest.main()
