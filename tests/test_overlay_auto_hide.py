"""Regression tests for idle fade/auto-hide setting and saved state.

Avoid starting audio capture or a model server.
"""
import os
import queue
import runpy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
MOD = runpy.run_path("live-caption-ja.pyw", run_name="overlay_auto_hide_tests")
Overlay = MOD["OverlaySink"]


class Var:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


def blank_overlay(auto_hide=True, caption="字幕"):
    o = Overlay.__new__(Overlay)
    o.root = Mock()
    o.root.after = Mock()
    o.closed = False
    o.fading = False
    o._fade_generation = 0
    o.alpha = .82
    o.auto_hide = auto_hide
    o.finals = [caption] if caption else []
    o.partial_text = ""
    o.translation_text = ""
    o.translation_source = ""
    o.caption_seen = bool(caption)
    o.last_update = time.time() - 30
    o.hold = 1.0
    o.history_count = 1
    o.show_previous = True
    o.q = queue.Queue()
    o.l_prev = Mock()
    o.l_cur = Mock()
    o.l_trans = Mock()
    o._setting_vars = {"auto_hide": Var(auto_hide)}
    o._schedule_save = Mock()
    # _current_settings touches all saved scalar preferences; fill only the
    # unrelated ones with harmless values in the isolated mock.
    for key in ("asr_model context_enabled context_history context_chars llama_ctx "
                "slots max_tokens temperature partial_every stable_count "
                "punct_silence hesitation_silence silence vad_win vad_threshold "
                "vad_min_speech_ms vad_min_silence_ms vad_speech_pad_ms min_seg "
                "max_seg max_gain font_name font_size width_frac window_height "
                "bottom current_color translation_color previous_color "
                "background_color topmost current_bold").split():
        if not hasattr(o, key):
            setattr(o, key, 1)
    return o


class OverlayAutoHideTests(unittest.TestCase):
    def test_disabled_auto_hide_does_not_start_fade_after_timeout(self):
        overlay = blank_overlay(auto_hide=False)
        with patch.object(overlay, "_start_fade") as start:
            overlay._poll()
        start.assert_not_called()
        overlay.root.attributes.assert_not_called()

    def test_enabled_auto_hide_still_fades_after_timeout(self):
        overlay = blank_overlay(auto_hide=True)
        with patch.object(overlay, "_start_fade") as start:
            overlay._poll()
        start.assert_called_once_with()

    def test_turning_off_restores_fully_invisible_overlay_immediately(self):
        overlay = blank_overlay(auto_hide=True)
        overlay.root.attributes("-alpha", 0)
        overlay._setting_vars["auto_hide"].set(False)
        overlay._settings_changed("auto_hide")
        self.assertFalse(overlay.auto_hide)
        self.assertFalse(overlay.fading)
        overlay.root.attributes.assert_any_call("-alpha", .82)
        overlay._schedule_save.assert_called_once()

    def test_turning_off_during_fade_cancels_pending_steps(self):
        overlay = blank_overlay(auto_hide=True)
        scheduled = []
        overlay.root.after.side_effect = lambda ms, fn: scheduled.append(fn)
        overlay._start_fade()
        self.assertTrue(overlay.fading)
        self.assertTrue(scheduled)
        overlay._setting_vars["auto_hide"].set(False)
        overlay._settings_changed("auto_hide")
        overlay.root.attributes.reset_mock()
        for callback in scheduled[:]:
            callback()
        self.assertFalse(overlay.fading)
        overlay.root.attributes.assert_not_called()

    def test_old_fade_callbacks_cannot_hide_after_reenable(self):
        overlay = blank_overlay(auto_hide=True)
        scheduled = []
        overlay.root.after.side_effect = lambda ms, fn: scheduled.append(fn)
        overlay._start_fade()
        old_callback = scheduled[-1]
        overlay._setting_vars["auto_hide"].set(False)
        overlay._settings_changed("auto_hide")
        overlay._setting_vars["auto_hide"].set(True)
        overlay._settings_changed("auto_hide")
        overlay.last_update = time.time() - 20
        overlay._start_fade()
        overlay.root.attributes.reset_mock()
        old_callback()
        overlay.root.attributes.assert_not_called()

    def test_saved_settings_include_auto_hide_value(self):
        overlay = blank_overlay(auto_hide=False)
        settings = overlay._current_settings()
        self.assertIs(settings["auto_hide"], False)

    def test_auto_hide_setting_round_trips_through_json(self):
        overlay = blank_overlay(auto_hide=False)
        with tempfile.TemporaryDirectory(prefix="overlay_auto_hide_setting_") as folder:
            overlay.settings_path = str(Path(folder) / "settings.json")
            overlay._save_settings()
            loaded = overlay._load_settings({"auto_hide": True})
            self.assertIs(loaded["auto_hide"], False)
            overlay.auto_hide = True
            overlay._save_settings()
            reloaded = overlay._load_settings({"auto_hide": False})
            self.assertIs(reloaded["auto_hide"], True)

    def test_complete_fade_then_disable_restores_window_controls(self):
        overlay = blank_overlay(auto_hide=True)
        callbacks = []
        overlay.root.after.side_effect = lambda delay, fn: callbacks.append(fn)
        overlay._start_fade()
        for i in range(21):
            self.assertTrue(callbacks)
            callbacks.pop(0)()
        overlay.root.attributes.assert_any_call("-alpha", 0.0)
        self.assertFalse(overlay.fading)
        overlay._setting_vars["auto_hide"].set(False)
        overlay._settings_changed("auto_hide")
        overlay.root.attributes.assert_any_call("-alpha", overlay.alpha)
        with patch.object(overlay, "_start_fade") as start:
            overlay._poll()
        start.assert_not_called()

    @unittest.skipIf(os.environ.get("GITHUB_ACTIONS") == "true",
                     "Real Tk widgets require an interactive Windows desktop")
    def test_new_overlay_defaults_to_auto_hide_and_settings_checkbox_exists(self):
        # Real Tk widget construction verifies the checkbox is actually shown
        # and that the setting is bound to its variable.
        with patch.object(Overlay, "_load_settings", autospec=True,
                          side_effect=lambda self, d: dict(d)), \
             patch.object(Overlay, "_schedule_save"):
            overlay = Overlay()
            try:
                overlay.root.withdraw()
                self.assertTrue(overlay.auto_hide)
                overlay.open_settings()
                overlay.settings_win.withdraw()
                controls = []
                def descendants(widget):
                    for child in widget.winfo_children():
                        controls.append(child)
                        descendants(child)
                descendants(overlay.settings_win)
                check = next(w for w in controls
                             if w.winfo_class()=="Checkbutton" and
                             "空闲自动隐藏" in w.cget("text"))
                self.assertTrue(overlay._setting_vars["auto_hide"].get())
                self.assertEqual(str(check.cget("variable")),
                                 str(overlay._setting_vars["auto_hide"]))
                check.invoke()  # Actual checkbox click executes command callback.
                self.assertFalse(overlay.auto_hide)
                overlay._reset_settings()
                self.assertTrue(overlay.auto_hide)
                self.assertTrue(overlay._setting_vars["auto_hide"].get())
            finally:
                overlay.root.destroy()


if __name__=="__main__":
    unittest.main()
