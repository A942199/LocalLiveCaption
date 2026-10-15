"""Python implementation of upstream Translator/TranslationTaskQueue scheduling.

The public sink accepts existing ASR partial/final strings unchanged. The
ASR workers, VAD, model inference and audio path are entirely independent.
"""
import queue
from contextlib import nullcontext
from collections import deque
import re
import threading
import time
import weakref
from pathlib import Path

from .api import (TranslationAPI, TranslationSettings, LLM_ENGINES,
                  CancellationToken, TranslationCancelled)
from .history import HistoryStore
from .text import (PUNC_EOS, SHORT_THRESHOLD, NOTICE, preprocess,
                   extract_original_caption, latest_caption, similar_to_last)

ROOT = Path(__file__).resolve().parent.parent
ACTIVE_SINKS = weakref.WeakSet()


class GoogleTranslateSink:
    """Backward-compatible sink name; supports all 11 upstream translation APIs."""
    PUNC_EOS = PUNC_EOS
    SHORT_THRESHOLD = SHORT_THRESHOLD
    MAX_SYNC_INTERVAL = 3
    MAX_IDLE_INTERVAL = 50
    SYNC_TICK = 0.025
    TRANSLATE_TICK = 0.040
    DISPLAY_TICK = 0.040
    COMPLETE_SENTENCE_CHOKE = 0.720

    def __init__(self, overlay, target="zh-CN", settings=None, api=None,
                 history=None, root=None):
        self.overlay = overlay
        self.closed = False
        root = Path(root or ROOT)
        if settings is None:
            try:
                settings = TranslationSettings.from_file(root / "translation-settings.json")
            except (OSError, ValueError, TypeError) as exc:
                print("翻译配置无效，使用默认 Google 日译中：", exc, flush=True)
                settings = TranslationSettings()
        # Old caller always passed target='zh-CN'; don't override a custom target.
        if settings.target == "zh-CN" and target != "zh-CN":
            settings.target = target
        self.settings = settings
        self.target = settings.target
        self.api = api or TranslationAPI(settings)
        self.history = history or HistoryStore(root / "translation-history.sqlite3")

        self.state_lock = threading.Lock()
        self.observed_text = ""
        self.finalized_text = ""
        self.context_entries = deque(maxlen=10)
        self.original_caption = ""
        self.idle_count = 0
        self.sync_count = 0
        self.pending_text_queue = queue.Queue()
        self.task_lock = threading.Lock()
        self.tasks = []
        self.next_task_id = 0
        self.output_lock = threading.Lock()
        self.output_version = 0
        self.output = ("", "", False)

        self.sync_thread = threading.Thread(target=self._sync_loop,daemon=True,
                                            name="translation-sync")
        self.translate_thread = threading.Thread(target=self._translate_loop,daemon=True,
                                                 name="translation-dispatch")
        self.display_thread = threading.Thread(target=self._display_loop,daemon=True,
                                               name="translation-display")
        self.sync_thread.start()
        self.translate_thread.start()
        self.display_thread.start()
        ACTIVE_SINKS.add(self)

    @classmethod
    def _extract_original_caption(cls, text):
        return extract_original_caption(text)

    def _observe(self,text):
        with self.state_lock:
            self.observed_text = text or ""

    def partial(self,text):
        # LiveCaptions provides cumulative text; Qwen3-ASR provides one segment.
        # Reconstruct the cumulative transcript on the TRANSLATION side only.
        with self.state_lock:
            self.observed_text = getattr(self,"finalized_text","") + (text or "")

    def final(self,text,a=None,b=None):
        if not text or not text.strip():
            return
        with self.state_lock:
            source=text.strip()
            # Endpointing establishes a sentence boundary even when Qwen3-ASR
            # did not emit punctuation; the original ASR result is untouched.
            if source[-1] not in PUNC_EOS:
                source += "。"
            self.finalized_text = (getattr(self,"finalized_text","") + source)[-8192:]
            self.observed_text = self.finalized_text

    def _sync_once(self):
        with self.state_lock:
            original = self.observed_text
            if not original:
                return None
            full_text = preprocess(original)
            if not any(mark in full_text for mark in PUNC_EOS):
                queue_ = getattr(self,"context_entries",None)
                if queue_ is not None:
                    queue_.clear()
            latest = extract_original_caption(full_text)
            pending = None
            if latest != self.original_caption:
                self.original_caption = latest
                self.idle_count = 0
                if latest and latest[-1] in self.PUNC_EOS:
                    self.sync_count = 0
                    pending = latest
                elif len(latest.encode("utf-8")) >= self.SHORT_THRESHOLD:
                    self.sync_count += 1
            else:
                self.idle_count += 1
            if self.sync_count > self.MAX_SYNC_INTERVAL or self.idle_count == self.MAX_IDLE_INTERVAL:
                self.sync_count = 0
                pending = self.original_caption
        if pending:
            self.pending_text_queue.put(pending)
        return pending

    def _sync_loop(self):
        while not self.closed:
            self._sync_once()
            time.sleep(self.SYNC_TICK)

    def _translate_loop(self):
        while not self.closed:
            try:
                source = self.pending_text_queue.get_nowait()
            except queue.Empty:
                source = ""
            if source:
                if self.settings.log_only:
                    try:
                        self.history.log(source,"N/A","N/A","LogOnly")
                        self.overlay.translation(source,"[Paused]")
                    except Exception as exc:
                        print("翻译历史保存失败：",repr(exc),flush=True)
                else:
                    self._enqueue_translation_task(source)
            time.sleep(self.TRANSLATE_TICK)

    def _enqueue_translation_task(self,source):
        with self.task_lock:
            if self.closed:
                return None
            self.next_task_id += 1
            task = {"id":self.next_task_id,"text":source,"cancelled":False,
                    "cancel_token":CancellationToken()}
            self.tasks.append(task)
        thread=threading.Thread(target=self._translation_worker,
                                args=(task,),daemon=True,
                                name=f"translation-job-{task['id']}")
        task["thread"]=thread
        thread.start()
        return task

    def _translation_worker(self,task):
        text = task["text"]
        choke = bool(text and text[-1] in PUNC_EOS)
        with self.state_lock:
            contexts = (list(self.context_entries)[-self.settings.num_contexts:]
                        if self.settings.context_aware and self.settings.num_contexts>0
                        else [])
        api=self.api
        settings=self.settings
        token=task.get("cancel_token")
        context=(api.cancellation(token) if isinstance(api,TranslationAPI)
                 else nullcontext())
        try:
            with context:
                if settings.context_aware and settings.engine not in LLM_ENGINES:
                    previous = self.history.previous_text(
                        settings.num_contexts,translated=False,entries=contexts)
                    raw = api.translate(f"{previous} 🔤 {text} 🔤",contexts)
                    if "[ERROR]" in raw or "[WARNING]" in raw:
                        translated = raw
                    else:
                        match = re.search(r"🔤\s*(.+?)\s*🔤",raw)
                        translated = match.group(1) if match else ""
                else:
                    translated = api.translate(text,contexts).replace("🔤","")
        except TranslationCancelled:
            return
        except Exception as error:
            translated = "[ERROR] Translation Failed: "+str(error)
        with self.task_lock:
            if task.get("cancelled") or task not in self.tasks or self.closed:
                return
        self._on_task_completed(task,translated,choke)

    def _on_task_completed(self,task,translated,choke):
        with self.task_lock:
            if task.get("cancelled") or task not in self.tasks or self.closed:
                return False
            index=self.tasks.index(task)
            for older in self.tasks[:index]:
                older["cancelled"]=True
                token=older.get("cancel_token")
                if token is not None:
                    token.cancel()
            del self.tasks[:index+1]
            # Keep task state and published result together. The upstream
            # intends only the latest completed task to replace prior output.
            with self.output_lock:
                self.output=(task["text"],translated,choke)
                self.output_version += 1
        try:
            previous=self.history.last()
            if not isinstance(getattr(previous,"source",None),str):
                previous=None
            overwrite=bool(previous and similar_to_last(task["text"],previous.source))
            # Upstream AddContexts() queues the previous log *before* the
            # current (non-overwrite) record is committed.
            if not overwrite and previous:
                with self.state_lock:
                    queue_=getattr(self,"context_entries",None)
                    if queue_ is not None:
                        queue_.append(previous)
            self.history.log(task["text"],translated,self.settings.target,
                             self.settings.engine,overwrite=overwrite)
        except Exception as error:
            print("翻译历史保存失败：",repr(error),flush=True)
        return True

    def _display_loop(self):
        seen=0
        while not self.closed:
            if self.settings.log_only:
                time.sleep(self.DISPLAY_TICK)
                continue
            with self.output_lock:
                version=self.output_version
                result=self.output
            if version!=seen:
                seen=version
                source,translated,choke=result
                show=getattr(self.settings,"display_sentences",0)
                if show>0 and "[ERROR]" not in translated and "[WARNING]" not in translated:
                    with self.state_lock:
                        previous=list(getattr(self,"context_entries",[]))[-show:]
                    prefix=self.history.previous_text(show,entries=previous)
                    if prefix:
                        translated=prefix+translated
                self.overlay.translation(source,translated)
                if choke:
                    time.sleep(self.COMPLETE_SENTENCE_CHOKE)
            time.sleep(self.DISPLAY_TICK)

    def update_settings(self,settings):
        """Apply configuration without leaking results from the old engine.

        Cancel active HTTP connections for obsolete translation tasks,
        without changing or restarting ASR/VAD/audio workers.
        """
        with self.task_lock:
            for task in self.tasks:
                task["cancelled"]=True
                token=task.get("cancel_token")
                if token is not None:
                    token.cancel()
            self.tasks.clear()
            pending=getattr(self,"pending_text_queue",None)
            if pending is not None:
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
            with self.state_lock:
                changed=(self.settings.engine!=settings.engine or
                         self.settings.target!=settings.target)
                self.settings=settings
                self.target=settings.target
                if isinstance(self.api,TranslationAPI):
                    # Do not mutate a request object another worker may be
                    # using. Rebuild it with the same transport hook.
                    self.api=TranslationAPI(settings,requester=self.api.requester)
                else:
                    self.api.settings=settings
                if changed:
                    self.context_entries.clear()
                    if hasattr(self,"original_caption"):
                        self.original_caption=""
                    if hasattr(self,"idle_count"):
                        self.idle_count=0
                    if hasattr(self,"sync_count"):
                        self.sync_count=0
            with self.output_lock:
                self.output=("", "", False)
                self.output_version+=1
        overlay=getattr(self,"overlay",None)
        if overlay is not None:
            overlay.translation("","[Paused]" if settings.log_only else "")

    def set_log_only(self,value):
        self.settings.log_only=bool(value)
        if value:
            self.overlay.translation("","[Paused]")

    def close(self):
        ACTIVE_SINKS.discard(self)
        self.closed=True
        with self.task_lock:
            for task in self.tasks:
                task["cancelled"]=True
                token=task.get("cancel_token")
                if token is not None:
                    token.cancel()
            self.tasks.clear()
        for worker in (self.sync_thread,self.translate_thread,self.display_thread):
            if worker is not threading.current_thread():
                worker.join(timeout=0.5)
