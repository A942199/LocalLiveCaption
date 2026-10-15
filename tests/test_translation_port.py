"""Offline parity/regression tests of upstream translation Python port."""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from translation import GoogleTranslateSink, HistoryStore, TranslationAPI, TranslationSettings
from translation.api import ENGINES, GOOGLE_URL
from translation.text import preprocess, extract_original_caption, similar_to_last


class TranslationTextTests(unittest.TestCase):
    def test_preprocessing_newlines(self):
        self.assertEqual(preprocess("こんにちは\n世界"),"こんにちは——世界")
        self.assertEqual(preprocess("いま は 晴れ。  そうです！"),"いま は 晴れ。そうです！")

    def test_sentence_extraction(self):
        self.assertEqual(extract_original_caption("昨日です。今日は晴れです。"),"今日は晴れです。")
        self.assertEqual(extract_original_caption("今日は晴れ"),"今日は晴れ")

    def test_similar_last(self):
        self.assertTrue(similar_to_last("今日は暖かい","今日は暖かいですよ"))
        self.assertFalse(similar_to_last("りんご","あいうえお"))


class HistoryTests(unittest.TestCase):
    def test_log_overwrite_and_contexts(self):
        with tempfile.TemporaryDirectory() as folder:
            db=HistoryStore(Path(folder)/"history.sqlite")
            self.assertFalse(db.log("明日は晴れ","明天晴","zh-CN","Google"))
            self.assertTrue(db.log("明日は晴れです","明天是晴天","zh-CN","Google"))
            self.assertEqual(1,len(db.recent()))
            self.assertEqual("明日は晴れです",db.last().source)
            self.assertEqual("明天是晴天。",db.previous_text())
            db.log("雨が降ります","[ERROR] error","zh-CN","Google")
            self.assertEqual(1,len(db.contexts()))


class ApiTests(unittest.TestCase):
    def call(self,engine,answer,config=None):
        calls=[]
        def fake(url,**kw):
            calls.append((url,kw))
            return answer
        settings=TranslationSettings(engine=engine,configs={engine:config or {}})
        output=TranslationAPI(settings,requester=fake).translate("こんにちは")
        self.assertEqual(1,len(calls),engine)
        self.assertEqual(8.0,calls[0][1]["timeout"])
        return output,calls[0]

    def test_all_eleven_engines_mocked(self):
        cases={
          "Google":([[ "你好" ]],"你好"),
          "Google2":({"translateResponse":{"translateText":"你好"}},"你好"),
          "Ollama":({"message":{"content":"你好"}},"你好"),
          "OpenAI":({"choices":[{"message":{"content":"你好"}}]},"你好"),
          "LMStudio":({"output":[{"type":"message","content":"你好"}]},"你好"),
          "DeepL":({"translations":[{"text":"你好"}]},"你好"),
          "OpenRouter":({"choices":[{"message":{"content":"你好"}}]},"你好"),
          "Youdao":({"errorCode":"0","translation":["你好"]},"你好"),
          "MTranServer":({"result":"你好"},"你好"),
          "Baidu":({"trans_result":[{"dst":"你好"}]},"你好"),
          "LibreTranslate":({"translatedText":"你好"},"你好"),
        }
        self.assertEqual(set(ENGINES),set(cases))
        for engine,(answer,expected) in cases.items():
            with self.subTest(engine=engine):
                text,_=self.call(engine,answer)
                self.assertEqual(expected,text)

    def test_google_wire_protocol(self):
        text,(url,kw)=self.call("Google",[["你好"]])
        self.assertTrue(url.startswith(GOOGLE_URL+"?"))
        self.assertIn("client=dict-chrome-ex",url)
        self.assertIn("tl=zh-CN",url)
        self.assertIn("q=%E3%81%93",url)

    def test_llm_context_messages(self):
        settings=TranslationSettings(engine="OpenAI",context_aware=True)
        api=TranslationAPI(settings,requester=lambda *a,**k:{"choices":[{"message":{"content":"done"}}]})
        context=Mock(source="昨日です",translated="昨天")
        answer=api.translate("今日は晴れ",contexts=[context])
        self.assertEqual("done",answer)
        messages=api._messages("今日は晴れ",[context])
        self.assertEqual(4,len(messages))
        self.assertEqual("🔤 昨日です 🔤",messages[1]["content"])

    def test_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/"settings.json"
            file.write_text(json.dumps({"engine":"bad"}),encoding="utf-8")
            with self.assertRaises(ValueError):
                TranslationSettings.from_file(file)


class SchedulerTests(unittest.TestCase):
    def scheduler(self):
        t=GoogleTranslateSink.__new__(GoogleTranslateSink)
        t.state_lock=threading.Lock()
        t.observed_text=""
        t.original_caption=""
        t.idle_count=0
        t.sync_count=0
        t.pending_text_queue=queue.Queue()
        return t

    def test_punctuation_immediate(self):
        t=self.scheduler()
        t.partial("今日はいい天気ですね。")
        self.assertEqual("今日はいい天気ですね。",t._sync_once())

    def test_four_changes_and_50_idle_ticks(self):
        t=self.scheduler()
        for src in ("今日は天気","今日は天気が","今日は天気がとても","今日は天気がとても良い"):
            t.partial(src)
            answer=t._sync_once()
        self.assertEqual("今日は天気がとても良い",answer)
        t=self.scheduler()
        t.partial("こんにちは世界")
        t._sync_once()
        for i in range(49):
            self.assertIsNone(t._sync_once())
        self.assertEqual("こんにちは世界",t._sync_once())

    def test_newer_completion_prevents_old_overwrite(self):
        t=self.scheduler()
        t.closed=False
        t.task_lock=threading.Lock()
        t.output_lock=threading.Lock()
        t.output_version=0
        t.output=("","",False)
        t.history=Mock()
        t.settings=TranslationSettings()
        old={"text":"古い","cancelled":False}
        new={"text":"新しい","cancelled":False}
        t.tasks=[old,new]
        self.assertTrue(t._on_task_completed(new,"新的",False))
        self.assertFalse(t._on_task_completed(old,"旧的",False))
        self.assertEqual(("新しい","新的",False),t.output)
        self.assertTrue(old["cancelled"])


if __name__=="__main__":
    unittest.main()
