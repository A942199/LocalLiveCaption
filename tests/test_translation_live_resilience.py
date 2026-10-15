"""Fault-injection regression tests for translation-only runtime.

These cover two bugs uncovered by the live Google verification follow-up:
errors disappearing with context enabled; stale results after hot settings.
No ASR, network, filesystem-backed user settings, or GPU dependency.
"""
from collections import deque
import queue
import threading
import unittest
from unittest.mock import Mock

from translation.api import TranslationSettings
from translation.engine import GoogleTranslateSink


class FakeOverlay:
    def __init__(self):
        self.events=[]
    def translation(self,source,translated):
        self.events.append((source,translated))


class FakeAPI:
    def __init__(self,result):
        self.result=result
        self.settings=TranslationSettings()
    def translate(self,source,contexts=()):
        return self.result


def isolated_sink(*, context_aware=False,api_result="[ERROR] Translation Failed: HTTP Error - 503"):
    sink=GoogleTranslateSink.__new__(GoogleTranslateSink)
    sink.closed=False
    sink.settings=TranslationSettings(engine="Google",context_aware=context_aware)
    sink.target=sink.settings.target
    sink.state_lock=threading.Lock()
    sink.task_lock=threading.Lock()
    sink.output_lock=threading.Lock()
    sink.pending_text_queue=queue.Queue()
    sink.context_entries=deque(maxlen=10)
    sink.tasks=[]
    sink.output=("", "", False)
    sink.output_version=0
    sink.next_task_id=0
    sink.original_caption=""
    sink.observed_text=""
    sink.overlay=FakeOverlay()
    sink.history=Mock()
    sink.history.last.return_value=None
    sink.history.previous_text.return_value="昨日は雨でした。"
    sink.api=FakeAPI(api_result)
    return sink


class TranslationFaultRegressions(unittest.TestCase):
    def test_non_llm_error_survives_context_marker_extraction(self):
        t=isolated_sink(context_aware=True)
        task={"text":"今日は晴れです。","cancelled":False}
        t.tasks=[task]
        t._translation_worker(task)
        self.assertEqual("[ERROR] Translation Failed: HTTP Error - 503",t.output[1])
        self.assertEqual("[ERROR] Translation Failed: HTTP Error - 503",
                         t.history.log.call_args.args[1])

    def test_settings_hot_swap_cancels_old_result(self):
        t=isolated_sink()
        task={"text":"前のエンジンです。","cancelled":False}
        t.tasks=[task]
        t.pending_text_queue.put("old pending caption")
        changed=TranslationSettings(engine="Google2",target="zh-TW")
        t.update_settings(changed)
        self.assertTrue(task["cancelled"])
        self.assertFalse(t._on_task_completed(task,"来自旧引擎",True))
        self.assertNotIn("来自旧引擎",t.output)
        self.assertTrue(t.pending_text_queue.empty())
        t.history.log.assert_not_called()
        self.assertEqual("Google2",t.api.settings.engine)

    def test_in_flight_worker_result_ignored_after_hot_swap(self):
        t=isolated_sink()
        waiting=threading.Event()
        release=threading.Event()
        class SlowAPI:
            def __init__(self):
                self.settings=t.settings
            def translate(self,*args,**kwargs):
                waiting.set()
                release.wait(timeout=3)
                return "旧翻译"
        t.api=SlowAPI()
        task={"text":"古い文です。","cancelled":False}
        t.tasks=[task]
        worker=threading.Thread(target=t._translation_worker,args=(task,))
        worker.start()
        try:
            self.assertTrue(waiting.wait(timeout=2))
            t.update_settings(TranslationSettings(engine="Google2"))
        finally:
            release.set()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("旧翻译",t.output)
        t.history.log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
