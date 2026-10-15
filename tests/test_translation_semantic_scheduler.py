"""Deterministic regression tests for stable preview, VAD joining and updates."""
from collections import deque
from unittest.mock import Mock
import queue
import threading
import unittest

from translation.api import TranslationSettings
from translation.engine import GoogleTranslateSink


def sink():
    s=GoogleTranslateSink.__new__(GoogleTranslateSink)
    s.state_lock=threading.Lock()
    s.task_lock=threading.Lock()
    s.output_lock=threading.Lock()
    s.closed=False
    s.observed_text=""
    s.finalized_text=""
    s.original_caption=""
    s.idle_count=0
    s.sync_count=0
    s.final_generation=0
    s.final_dispatched_generation=0
    s.last_queued_key=None
    s.last_final_at=0.
    s.last_preview_at=0.
    s.last_final_marker=None
    s.context_entries=deque(maxlen=10)
    s.pending_text_queue=queue.Queue()
    s.tasks=[]
    s.next_task_id=0
    s.output=("","",False)
    s.output_version=0
    s.settings=TranslationSettings()
    s.history=Mock()
    s.history.last.return_value=None
    s.overlay=Mock()
    s.preview_cache=None
    return s


class SemanticSchedulerTests(unittest.TestCase):
    def test_stable_partial_only_and_throttle(self):
        s=sink()
        s.partial_stable("今日は晴れですが", "")
        self.assertIsNone(s._sync_once(now=100))
        s.partial_stable("今日は晴れですが", "今日は晴れ")
        self.assertIsNone(s._sync_once(now=101))
        s.partial_stable("今日は晴れですが", "今日は晴れです。")
        self.assertEqual("今日は晴れです。",s._sync_once(now=102))
        self.assertEqual(("今日は晴れです。","preview",0),
                         s.pending_text_queue.get_nowait())
        for i in range(60):
            self.assertIsNone(s._sync_once(now=102.1+i*.025))
        self.assertTrue(s.pending_text_queue.empty())

    def test_two_finals_join_before_translation(self):
        s=sink()
        s.final("昨日、友達と映画を",1.,2.)
        self.assertIsNone(s._sync_once(now=s.last_final_at+.1))
        s.final("見に行きました。",2.,3.)
        self.assertEqual("昨日、友達と映画を見に行きました。",
                         s._sync_once(now=s.last_final_at+.1))
        self.assertEqual(("昨日、友達と映画を見に行きました。","final",2),
                         s.pending_text_queue.get_nowait())

    def test_unpunctuated_final_flush_once_after_grace(self):
        s=sink()
        s.final("明日の予定がまだ決まっていない",1.,2.)
        self.assertIsNone(s._sync_once(now=s.last_final_at+.1))
        self.assertEqual("明日の予定がまだ決まっていない",
                         s._sync_once(now=s.last_final_at+1.))
        self.assertEqual(("明日の予定がまだ決まっていない","final",1),
                         s.pending_text_queue.get_nowait())
        for i in range(60):
            self.assertIsNone(s._sync_once(now=s.last_final_at+2+i*.025))
        self.assertTrue(s.pending_text_queue.empty())

    def test_timeout_ends_translation_unit_not_next_sentence(self):
        s=sink()
        s.final("昨日、映画を見た",1.,2.)
        self.assertEqual("昨日、映画を見た",
                         s._sync_once(now=s.last_final_at+1.))
        s.final("今日は晴れました。",4.,5.)
        self.assertEqual("今日は晴れました。",
                         s._sync_once(now=s.last_final_at+.1))

    def test_stable_continuation_keeps_previous_final_open(self):
        s=sink()
        s.final("昨日友達と映画を",1.,2.)
        s.partial_stable("見に行きました", "見に行き")
        self.assertIsNone(s._sync_once(now=s.last_final_at+1.))
        s.final("見に行きました。",2.,3.)
        self.assertEqual("昨日友達と映画を見に行きました。",
                         s._sync_once(now=s.last_final_at+.1))

    def test_duplicate_final_marker_does_not_duplicate_text(self):
        s=sink()
        s.final("いい天気。",1.,2.)
        s.final("いい天気。",1.,2.)
        self.assertEqual("いい天気。",s.finalized_text)
        self.assertEqual(1,s.final_generation)

    def test_preview_not_logged_and_final_reuses_identical_preview(self):
        s=sink()
        preview={"id":1,"text":"良い天気です。","kind":"preview",
                 "generation":0,"cancelled":False}
        s.tasks=[preview]
        self.assertTrue(s._on_task_completed(preview,"天气很好。",True))
        s.history.log.assert_not_called()
        s.final_generation=1
        s._enqueue_translation_task("良い天気です。","final",1)
        self.assertEqual(("良い天気です。","天气很好。",True),s.output)
        s.history.log.assert_called_once()

    def test_stale_preview_rejected_after_new_final(self):
        s=sink()
        preview={"id":1,"text":"古い速報","kind":"preview",
                 "generation":0,"cancelled":False}
        s.tasks=[preview]
        s.final("新しい文章。",1.,2.)
        self.assertFalse(s._on_task_completed(preview,"旧翻译",False))
        self.assertEqual(("","",False),s.output)
        s.history.log.assert_not_called()

    def test_hot_swap_does_not_send_empty_overlay_translation(self):
        s=sink()
        s.api=Mock()
        s.api.settings=s.settings
        s.target=s.settings.target
        s.output=("旧原文","旧译文",False)
        s.update_settings(TranslationSettings(engine="Google2"))
        self.assertEqual(("旧原文","旧译文",False),s.output)
        s.overlay.translation.assert_not_called()


if __name__=="__main__":
    unittest.main()
