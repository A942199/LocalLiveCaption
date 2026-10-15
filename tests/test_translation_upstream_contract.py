"""Cross-language contracts pinned to upstream C# commit a6fee12757b1.

No real API, microphone, or GPU needed. Intentionally fails when a previously
identified 1:1-port contract regresses.
"""
import csv
from contextlib import closing
from collections import deque
import json
from pathlib import Path
import queue
import re
import sqlite3
import tempfile
import threading
import unittest

from translation.api import TranslationAPI,TranslationSettings,ENGINES
from translation.engine import GoogleTranslateSink
from translation.gui import normalize_profile
from translation.history import HistoryStore, TranslationEntry
from translation.text import similarity

BASE=Path(__file__).resolve().parent.parent
UPSTREAM=(BASE/"src" if (BASE/"src"/"apis"/"TranslateAPI.cs").is_file()
          else BASE/"third_party"/"LiveCaptions-Translator"/"src")


class UpstreamSchemaContracts(unittest.TestCase):
    def test_provider_list_matches_upstream_source(self):
        source=(UPSTREAM/"apis"/"TranslateAPI.cs").read_text(encoding="utf-8-sig")
        block=source.split("TRANSLATE_FUNCTIONS = new()",1)[1].split("};",1)[0]
        names=re.findall(r'\{\s*"(\w+)"\s*,\s*\w+\s*\}',block)
        # The preserved C#/WPF branch predates upstream LMStudio support;
        # the Python port follows pinned a6fee127, which includes it.
        if UPSTREAM == BASE/"src":
            self.assertEqual(set(names),set(ENGINES)-{"LMStudio"})
        else:
            self.assertEqual(set(names),set(ENGINES))

    def test_deepl_all_13_language_codes_match_upstream(self):
        source=(UPSTREAM/"models"/"TranslateAPIConfig.cs").read_text(encoding="utf-8-sig")
        block=source.split("public class DeepLConfig",1)[1].split("private string apiKey",1)[0]
        expected=dict(re.findall(r'\{\s*"([^"]+)",\s*"([^"]+)"\s*\}',block))
        self.assertEqual(13,len(expected))
        for target,code in expected.items():
            with self.subTest(target=target):
                self.assertEqual(code,TranslationAPI(TranslationSettings(target=target))._lang("DeepL"))

    def test_all_target_tables_exact_source_mapping(self):
        source=(UPSTREAM/"models"/"TranslateAPIConfig.cs").read_text(encoding="utf-8-sig")
        for engine in ("DeepL","Youdao","MTranServer","Baidu","LibreTranslate"):
            with self.subTest(engine=engine):
                block=source.split("public class "+engine+"Config ",1)[1]
                table=block.split("SupportedLanguages => new()",1)[1].split("};",1)[0]
                mapping=dict(re.findall(r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\}',table))
                self.assertEqual(13,len(mapping))
                for language,expected in mapping.items():
                    actual=TranslationAPI(TranslationSettings(target=language))._lang(engine)
                    self.assertEqual(expected,actual,(engine,language))

    def test_request_schemas_match_upstream_discriminators(self):
        source=(UPSTREAM/"apis"/"LLMRequestDataFactory.cs").read_text(encoding="utf-8-sig")
        schemas=re.findall(r'\["([^"]+)"\]\s*=\s*typeof',source)
        self.assertEqual(["integrated","Aliyun","Anthropic","Ollama",
                          "OpenRouter","OpenAI","XAI","base"],schemas)
        for schema in schemas:
            with self.subTest(schema=schema):
                payload=TranslationAPI._request_data(schema,"model",[],0.5)
                self.assertIsInstance(payload["temperature"],float)
                self.assertEqual(128,payload["max_tokens"])
                self.assertEqual(600,payload["keep_alive"])
                self.assertIs(payload["stream"],False)
                if schema=="integrated":
                    self.assertIs(payload["think"],False)
                    self.assertIs(payload["reasoning"]["enabled"],False)
                if schema=="Ollama":
                    self.assertIs(payload["think"],False)
                if schema=="OpenRouter":
                    self.assertIs(payload["reasoning"]["exclude"],True)

    def test_openai_eight_fallbacks_for_400_and_422(self):
        for status in (400,422):
            calls=[]
            def requester(url,**kw):
                calls.append(kw["data"])
                if len(calls)<8:
                    raise ValueError(f"HTTP Error - {status}")
                return {"choices":[{"message":{"content":"译文"}}]}
            settings=TranslationSettings(engine="OpenAI",
                configs={"OpenAI":{"model_name":"test","api_url":"https://example.invalid/v1"}})
            self.assertEqual("译文",TranslationAPI(settings,requester=requester).translate("日本語"))
            self.assertEqual(8,len(calls))
            self.assertIn("thinking",calls[0])
            self.assertEqual({"effort":"low"},calls[5]["reasoning"])
            self.assertNotIn("reasoning",calls[-1])

    def test_openai_remembers_compatible_schema(self):
        calls=[]
        def fake(url,**kwargs):
            calls.append(kwargs["data"])
            if len(calls)==1:
                raise ValueError("HTTP Error - 400")
            return {"choices":[{"message":{"content":"ok"}}]}
        settings=TranslationSettings(engine="OpenAI",configs={
            "OpenAI":{"model_name":"mock","api_url":"https://example.invalid"}})
        api=TranslationAPI(settings,requester=fake)
        self.assertEqual("ok",api.translate("first"))
        self.assertEqual("ok",api.translate("second"))
        self.assertEqual(3,len(calls))
        self.assertIn("enable_thinking",calls[1])
        self.assertIn("enable_thinking",calls[2])

    def test_unicode_similarity_tracks_dotnet_utf16_code_units(self):
        self.assertAlmostEqual(2/3,similarity("😀a","😁a"))

    def test_normalized_url_and_timeout_match_upstream(self):
        from translation.api import normalize_url
        self.assertEqual("https://test.example/api/v1",
                         normalize_url("https://test.example//api///v1/"))
        def timeout(url,**kwargs):
            raise TimeoutError("timed out")
        message=TranslationAPI(TranslationSettings(engine="Google"),
                               requester=timeout).translate("こんにちは")
        self.assertIn("timeout (> 8 seconds)",message)

    def test_500_does_not_retry(self):
        calls=[]
        def requester(url,**kw):
            calls.append(url)
            raise ValueError("HTTP Error - 500")
        settings=TranslationSettings(engine="OpenAI",
            configs={"OpenAI":{"api_url":"https://example.invalid","model_name":"test"}})
        self.assertIn("500",TranslationAPI(settings,requester=requester).translate("x"))
        self.assertEqual(1,len(calls))

    def test_ollama_wire_model_matches_upstream(self):
        requests=[]
        def fake(url,**kw):
            requests.append(kw["data"])
            return {"message":{"content":"ok"}}
        settings=TranslationSettings(engine="Ollama",configs={
            "Ollama":{"model_name":"local","temperature":"0.7","keep_alive":"600"}})
        self.assertEqual("ok",TranslationAPI(settings,requester=fake).translate("x"))
        self.assertEqual(.7,requests[0]["temperature"])
        self.assertEqual(600,requests[0]["keep_alive"])
        self.assertEqual(128,requests[0]["max_tokens"])
        self.assertIs(requests[0]["think"],False)

    def test_gui_numeric_config_and_profiles(self):
        data=normalize_profile({"temperature":"0.65","keep_alive":"400",
                                "model_name":"test","api_key":"secret"})
        self.assertIsInstance(data["temperature"],float)
        self.assertIsInstance(data["keep_alive"],int)
        with self.assertRaises(ValueError):
            normalize_profile({"temperature":"not-a-number"})
        settings=TranslationSettings(engine="OpenAI",configs={"OpenAI":[
            {"model_name":"A"},{"model_name":"B"}]},config_indices={"OpenAI":1})
        self.assertEqual("B",settings.config()["model_name"])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"configs.json"
            from dataclasses import asdict
            path.write_text(json.dumps(asdict(settings)),encoding="utf-8")
            self.assertEqual("B",TranslationSettings.from_file(path).config()["model_name"])


class UpstreamHistoryContracts(unittest.TestCase):
    def test_bad_context_cards_not_passed_to_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/"history.sqlite3")
            entries=[
                TranslationEntry("t","昨日","昨天","zh-CN","Google"),
                TranslationEntry("t","失敗","[ERROR] Translation Failed","zh-CN","Google"),
            ]
            self.assertEqual("昨日。",store.previous_text(
                limit=2,translated=False,entries=entries))

    def test_import_upstream_schema_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"translation_history.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE TranslationHistory(
                    Id INTEGER PRIMARY KEY AUTOINCREMENT, Timestamp TEXT,
                    SourceText TEXT,TranslatedText TEXT,TargetLanguage TEXT,ApiUsed TEXT)""")
                db.execute("INSERT INTO TranslationHistory VALUES(1,'1660000000','こんにちは','你好','zh-CN','Google')")
            first=HistoryStore(path)
            self.assertEqual(1,len(first.recent()))
            self.assertEqual("こんにちは",first.last().source)
            self.assertEqual(1,len(HistoryStore(path).recent()))
            first.log("ありがとう","谢谢","zh-CN","Google")
            self.assertEqual(2,HistoryStore(path).count())

    def test_search_pagination_clear_and_export_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/"history.sqlite3")
            for i in range(1010):
                store.log(f"sentence {i}!","translated "+str(i),"zh-CN","Google",overwrite=False)
            self.assertEqual(1010,store.count())
            rows,pages=store.search("sentence",page=2,page_size=50)
            self.assertEqual(50,len(rows))
            self.assertEqual(21,pages)
            self.assertEqual(1,len(store.search("sentence 1009")[0]))
            path=Path(tmp)/"all.csv"
            self.assertEqual(1010,store.export_csv(path))
            with open(path,encoding="utf-8-sig",newline="") as fp:
                self.assertEqual(1011,len(list(csv.reader(fp))))
            store.clear()
            self.assertEqual(0,store.count())
            self.assertEqual(0,HistoryStore(Path(tmp)/"history.sqlite3").count())


class UpstreamSchedulerContracts(unittest.TestCase):
    def minimal(self):
        sink=GoogleTranslateSink.__new__(GoogleTranslateSink)
        sink.state_lock=threading.Lock()
        sink.task_lock=threading.Lock()
        sink.output_lock=threading.Lock()
        sink.output=("", "", False)
        sink.output_version=0
        sink.closed=False
        sink.original_caption=""
        sink.observed_text=""
        sink.finalized_text=""
        sink.idle_count=0
        sink.sync_count=0
        sink.context_entries=deque(maxlen=10)
        sink.pending_text_queue=queue.Queue()
        sink.settings=TranslationSettings()
        sink.tasks=[]
        return sink

    def test_final_segments_reconstructed_without_touching_asr(self):
        sink=self.minimal()
        sink.final("昨日は雨だった")
        self.assertEqual("昨日は雨だった。",sink._sync_once())
        sink.partial("今日は晴れ")
        self.assertEqual("昨日は雨だった。今日は晴れ",sink.observed_text)
        sink.final("今日は晴れ")
        self.assertEqual("昨日は雨だった。今日は晴れ。",sink.observed_text)
        self.assertEqual("今日は晴れ。",sink._sync_once())

    def test_context_queue_only_from_non_overwritten_previous_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink=self.minimal()
            sink.history=HistoryStore(Path(tmp)/"history.sqlite3")
            one={"text":"昨日は雨だった。","cancelled":False}
            sink.tasks=[one]
            self.assertTrue(sink._on_task_completed(one,"昨天下雨。",True))
            self.assertEqual(0,len(sink.context_entries))
            two={"text":"今日は晴れた。","cancelled":False}
            sink.tasks=[two]
            self.assertTrue(sink._on_task_completed(two,"今天晴了。",True))
            self.assertEqual("昨日は雨だった。",sink.context_entries[0].source)

    def test_hot_settings_keep_asr_untouched(self):
        sink=self.minimal()
        sink.api=TranslationAPI(sink.settings)
        sink.target="zh-CN"
        sink.context_entries.append(object())
        changed=TranslationSettings(engine="DeepL",target="fr-FR")
        sink.update_settings(changed)
        self.assertIs(sink.api.settings,changed)
        self.assertEqual("fr-FR",sink.target)
        self.assertFalse(sink.context_entries)


if __name__=="__main__":
    unittest.main()
