# Auto-generated single-file launcher for live-caption (Japanese mode).
# The original live_caption.py is embedded verbatim below; existing project files are untouched.
import os as _lc_os
import sys as _lc_sys
from pathlib import Path as _LCPath

_LC_ROOT = _LCPath(__file__).resolve().parent
_lc_os.chdir(_LC_ROOT)

# A .pyw double-click may use the system Python. Re-exec once into the project's venv,
# where all required packages are already installed.
if _lc_os.environ.get("_LIVE_CAPTION_PYW_BOOTSTRAPPED") != "1":
    _lc_pythonw = _LC_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not _lc_pythonw.is_file():
        try:
            import ctypes as _lc_ctypes
            _lc_ctypes.windll.user32.MessageBoxW(
                0,
                "Project virtual environment not found:\n" + str(_lc_pythonw) + "\n\nRun setup first.",
                "live-caption",
                0x10,
            )
        finally:
            raise SystemExit(1)
    _lc_env = dict(_lc_os.environ)
    _lc_env["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
    _lc_os.execve(
        str(_lc_pythonw),
        [str(_lc_pythonw), str(_LC_ROOT / _LCPath(__file__).name), *_lc_sys.argv[1:]],
        _lc_env,
    )

# pythonw has no console when launched by double-click. Keep ConsoleSink safe by
# redirecting missing standard streams to a UTF-8 log file instead of None.
_lc_log_stream = None
if _lc_sys.stdout is None or _lc_sys.stderr is None:
    _lc_log_stream = open(_LC_ROOT / "live-caption-pyw.log", "a", encoding="utf-8", buffering=1)
    if _lc_sys.stdout is None:
        _lc_sys.stdout = _lc_log_stream
    if _lc_sys.stderr is None:
        _lc_sys.stderr = _lc_log_stream
if _lc_sys.stdin is None:
    _lc_sys.stdin = open(_lc_os.devnull, "r", encoding="utf-8")

# Double-click defaults: fixed Japanese recognition and transcript output.
if "--lang" not in _lc_sys.argv:
    _lc_sys.argv += ["--lang", "ja"]
if "--out" not in _lc_sys.argv:
    _lc_sys.argv += ["--out", str(_LC_ROOT / "transcript.txt")]

# ==================== embedded original live_caption.py ====================
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
live_caption.py — 实时把「电脑正在播放的声音」转成字幕（本地 GPU，免费）

  采集: WASAPI loopback (pyaudiowpatch)，不需要 VB-Cable / 立体声混音
  识别: Qwen3-ASR-1.7B GGUF + llama.cpp（NVIDIA GPU）；Silero VAD 切句
  显示: 屏幕底部悬浮字幕条（可拖动、可调字号），同时也打到终端 / 文件

用法:
  python live_caption.py --lang zh                 # 中文字幕（Qwen3-ASR）
  python live_caption.py --lang zh --hotwords "BLG,T1,峡谷先锋,纳什男爵"   # 加专有名词（也可写在 hotwords.txt）
  python live_caption.py --list                    # 列出可用的输出设备
  python live_caption.py --device 7                # 指定输出设备(index)
  python live_caption.py --console                 # 不要悬浮窗，只在终端显示
  python live_caption.py --out notes.txt --srt notes.srt
字幕窗: 鼠标拖动移动位置；滚轮调字号；右键或 Esc 退出。终端 Ctrl+C 退出。
"""
import argparse
import os
import re
import sys
import time
import threading
import queue
import json
from urllib.parse import urlencode
from urllib.request import urlopen
from datetime import datetime, timedelta


import numpy as np
import pyaudiowpatch as pyaudio

SR = 16000  # 模型输入采样率


# ============================ 音频采集 ============================
def list_devices(pa):
    print("== WASAPI 输出设备 (loopback 可用) ==")
    try:
        api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        print("找不到 WASAPI"); return
    default = pa.get_device_info_by_index(api["defaultOutputDevice"])["name"]
    for d in pa.get_loopback_device_info_generator():
        mark = "  <- 当前默认" if default in d["name"] else ""
        print(f"  [{d['index']}] {d['name']}  ({int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} ch){mark}")
    print("用 --device <index> 指定；不指定则跟随系统默认输出设备。")


def get_loopback_device(pa, index=None):
    api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    out = pa.get_device_info_by_index(api["defaultOutputDevice"] if index is None else index)
    if out.get("isLoopbackDevice"):
        return out
    for lb in pa.get_loopback_device_info_generator():
        if out["name"] in lb["name"]:
            return lb
    raise RuntimeError(f"找不到 {out['name']} 的 loopback 设备，用 --list 看看")


class Resampler:
    def __init__(self, sr_in, sr_out=SR):
        self.sr_in, self.sr_out = sr_in, sr_out
        self.soxr = None
        if sr_in != sr_out:
            try:
                import soxr
                self.soxr = soxr.ResampleStream(sr_in, sr_out, 1, dtype="float32", quality="HQ")
            except ImportError:
                pass

    def __call__(self, mono):
        if self.sr_in == self.sr_out:
            return mono
        if self.soxr is not None:
            return self.soxr.resample_chunk(mono)
        n = int(len(mono) * self.sr_out / self.sr_in)
        return np.interp(np.linspace(0, len(mono), n, endpoint=False), np.arange(len(mono)), mono).astype(np.float32)


def start_capture(pa, dev, audio_q, stop):
    ch = int(dev["maxInputChannels"])
    sr = int(dev["defaultSampleRate"])
    rs = Resampler(sr)

    def cb(in_data, frame_count, time_info, status):
        if stop.is_set():
            return (None, pyaudio.paComplete)
        x = np.frombuffer(in_data, dtype=np.float32)
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        audio_q.put(rs(x.astype(np.float32)))
        return (None, pyaudio.paContinue)

    stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True,
                     input_device_index=dev["index"], frames_per_buffer=int(sr * 0.1), stream_callback=cb)
    stream.start_stream()
    return stream


# ============================ 输出：终端 / 文件 ============================
def fmt_ts(sec):
    td = timedelta(seconds=max(0.0, sec))
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{td.microseconds // 1000:03d}"


class ConsoleSink:
    def __init__(self, out_path=None, srt_path=None, show_partial=True):
        self.out = open(out_path, "a", encoding="utf-8") if out_path else None
        self.srt = open(srt_path, "a", encoding="utf-8") if srt_path else None
        self.show_partial = show_partial
        self.n_srt = 0
        self.t0 = time.time()
        self._plen = 0

    def partial(self, text):
        if not self.show_partial:
            return
        line = "  … " + text
        sys.stdout.write("\r" + line + " " * max(0, self._plen - len(line)))
        sys.stdout.flush()
        self._plen = len(line)

    def final(self, text, t_start, t_end):
        sys.stdout.write("\r" + " " * self._plen + "\r")
        self._plen = 0
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {text}", flush=True)
        if self.out:
            self.out.write(f"[{stamp}] {text}\n"); self.out.flush()
        if self.srt:
            self.n_srt += 1
            self.srt.write(f"{self.n_srt}\n{fmt_ts(t_start - self.t0)} --> {fmt_ts(t_end - self.t0)}\n{text}\n\n")
            self.srt.flush()

    def close(self):
        for f in (self.out, self.srt):
            if f:
                f.close()


# ============================ 输出：悬浮字幕条 ============================
class OverlaySink:
    """屏幕底部的视频式字幕条：最新一行正常显示，上一行缩小变灰；没新话时整体渐隐。
    必须在主线程创建/运行；识别线程通过 queue 投递文本。"""

    def __init__(self, font_size=30, width_frac=0.8, bottom=70, alpha=0.82, lines=2, hold=6.0, font="Microsoft YaHei"):
        import tkinter as tk
        import tkinter.font as tkfont
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        self.tk = tk
        self.q = queue.Queue()
        self.hold = hold
        self.alpha = alpha
        self.font_name = font
        self.font_size = font_size
        self.finals = []          # 最近两句最终结果
        self.partial_text = ""
        self.translation_text = ""
        self.translation_source = ""
        self.caption_seen = False    # 启动状态条在首句字幕出现前保持可见
        self.last_update = time.time()
        self.fading = False
        self.closed = False

        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", alpha)
        root.configure(bg="#000000")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.w = int(sw * width_frac)
        self.bottom = bottom
        self.sh = sh

        self.f_cur = tkfont.Font(family=font, size=font_size, weight="bold")
        self.f_trans = tkfont.Font(family=font, size=max(12, int(font_size * 0.82)), weight="normal")
        self.f_prev = tkfont.Font(family=font, size=max(10, int(font_size * 0.68)), weight="normal")
        self.l_prev = tk.Label(root, text="", fg="#9a9a9a", bg="#000000", justify="center", anchor="s",
                               font=self.f_prev, wraplength=self.w - 40, padx=20)
        self.l_cur = tk.Label(root, text="", fg="#ffffff", bg="#000000", justify="center", anchor="n",
                              font=self.f_cur, wraplength=self.w - 40, padx=20)
        self.l_trans = tk.Label(root, text="", fg="#d0d0d0", bg="#000000", justify="center", anchor="n",
                                font=self.f_trans, wraplength=self.w - 40, padx=20)
        self.l_prev.pack(side="top", fill="x", pady=(8, 0))
        self.l_cur.pack(side="top", fill="x", pady=(2, 0))
        self.l_trans.pack(side="top", fill="x", pady=(0, 10))
        self.hint = tk.Label(root, text="拖动=移动  滚轮=字号  Esc=退出", fg="#777777", bg="#000000", font=(font, 9))
        self.hint.place(relx=1.0, rely=0.0, x=-30, anchor="ne")
        self.close_btn = tk.Button(root, text="×", command=self.close, fg="#b0b0b0", bg="#000000",
                                   activeforeground="#ffffff", activebackground="#333333", relief="flat",
                                   bd=0, highlightthickness=0, padx=6, pady=0, font=("Segoe UI", 13, "bold"), cursor="hand2")
        self.close_btn.place(relx=1.0, rely=0.0, x=-4, y=1, anchor="ne")
        root.after(5000, self.hint.place_forget)
        self._relayout(first=True)

        for wdg in (root, self.l_prev, self.l_cur, self.l_trans):
            wdg.bind("<ButtonPress-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
            wdg.bind("<MouseWheel>", self._wheel)
        root.bind("<Escape>", lambda e: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(60, self._poll)

    # ---- 布局 ----
    def _relayout(self, first=False):
        # 高度按真实字体行高算：上一行 1 行 + 当前行最多 2 行（自动换行）
        h = (self.f_prev.metrics("linespace") + self.f_cur.metrics("linespace") * 2
             + self.f_trans.metrics("linespace") * 2 + 34)
        if first:
            x = (self.root.winfo_screenwidth() - self.w) // 2
            y = self.sh - h - self.bottom
            self.root.geometry(f"{self.w}x{h}+{x}+{y}")
        else:
            # 保持底边不动
            y_bottom = self.root.winfo_y() + self.root.winfo_height()
            self.root.geometry(f"{self.w}x{h}+{self.root.winfo_x()}+{y_bottom - h}")
        self.h = h

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _wheel(self, e):
        self.font_size = max(12, min(80, self.font_size + (2 if e.delta > 0 else -2)))
        self.f_cur.configure(size=self.font_size)
        self.f_trans.configure(size=max(12, int(self.font_size * 0.82)))
        self.f_prev.configure(size=max(10, int(self.font_size * 0.68)))
        self._relayout()

    # ---- 识别线程调用 ----
    def partial(self, text):
        self.q.put(("p", text))

    def final(self, text, t_start=None, t_end=None):
        self.q.put(("f", text))

    def translation(self, source, translated):
        self.q.put(("t", (source, translated)))

    def status(self, text):
        self.q.put(("s", text))

    def request_close(self):
        self.q.put(("q", ""))

    # ---- 主线程 ----
    def _poll(self):
        if self.closed:
            return
        changed = False
        while True:
            try:
                kind, text = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "q":
                self.close(); return
            if kind == "t":
                source, translated = text
                self.translation_source = source
                self.translation_text = translated
                changed = True
                self.last_update = time.time()
                continue
            changed = True
            if kind == "p":
                self.partial_text = text
                if text:
                    self.caption_seen = True
            elif kind == "s":
                self.finals, self.partial_text = [], text
                self.translation_text = ""
                self.translation_source = ""
            else:
                if text:
                    self.caption_seen = True
                    self.finals = (self.finals + [text])[-2:]
                self.partial_text = ""
            self.last_update = time.time()
        if changed:
            self._stop_fade()
            self.root.attributes("-alpha", self.alpha)
            self._render()
        elif self.caption_seen and (self.finals or self.partial_text) and not self.fading and time.time() - self.last_update > self.hold:
            self._start_fade()
        self.root.after(60, self._poll)

    def _render(self):
        if self.partial_text:
            cur, prev = self.partial_text, (self.finals[-1] if self.finals else "")
        else:
            cur = self.finals[-1] if self.finals else ""
            prev = self.finals[-2] if len(self.finals) > 1 else ""
        translated = self.translation_text
        self.l_prev.configure(text=prev)
        self.l_cur.configure(text=cur)
        self.l_trans.configure(text=translated)

    # ---- 渐隐：hold 秒没新字后 1.2s 内把整条淡出 ----
    def _start_fade(self):
        self.fading = True
        self._fade_step(0)

    def _fade_step(self, i):
        if not self.fading or self.closed:
            return
        n = 20
        if i > n:
            self.finals, self.partial_text = [], ""
            self.translation_text, self.translation_source = "", ""
            self._render()
            self.root.attributes("-alpha", 0.0)   # 整条隐掉，来新字再出现
            self.fading = False
            return
        self.root.attributes("-alpha", self.alpha * (1 - i / n))
        self.root.after(60, lambda: self._fade_step(i + 1))

    def _stop_fade(self):
        if self.fading:
            self.fading = False
            self.root.attributes("-alpha", self.alpha)

    def run(self):
        self.root.mainloop()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.root.destroy()
            except Exception:
                pass


_GOOGLE_TRANSLATE_URL = "https://clients5.google.com/translate_a/t"


def google_translate(text, target="zh-CN", timeout=8.0):
    """Google translation path used by SakiRinn/LiveCaptions-Translator (Google engine)."""
    query = urlencode({"client": "dict-chrome-ex", "sl": "auto", "tl": target, "q": text})
    with urlopen(f"{_GOOGLE_TRANSLATE_URL}?{query}", timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, str):
            return first
        if isinstance(first, list) and first and isinstance(first[0], str):
            return first[0]
    raise ValueError(f"unexpected Google Translate response: {payload!r}")


class GoogleTranslateSink:
    """Translation scheduler mirroring SakiRinn/LiveCaptions-Translator."""
    PUNC_EOS = ".?!。？！"
    SHORT_THRESHOLD = 10
    MAX_SYNC_INTERVAL = 3
    MAX_IDLE_INTERVAL = 50
    SYNC_TICK = 0.025
    TRANSLATE_TICK = 0.040
    DISPLAY_TICK = 0.040
    COMPLETE_SENTENCE_CHOKE = 0.720

    def __init__(self, overlay, target="zh-CN"):
        self.overlay = overlay
        self.target = target
        self.closed = False

        # SyncLoop-equivalent state.
        self.state_lock = threading.Lock()
        self.observed_text = ""
        self.original_caption = ""
        self.idle_count = 0
        self.sync_count = 0
        self.pending_text_queue = queue.Queue()

        # TranslationTaskQueue-equivalent state.
        self.task_lock = threading.Lock()
        self.tasks = []
        self.next_task_id = 0
        self.output_lock = threading.Lock()
        self.output_version = 0
        self.output = ("", "", False)  # source, translated, isChoke

        self.sync_thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="google-translate-sync")
        self.translate_thread = threading.Thread(
            target=self._translate_loop, daemon=True, name="google-translate-dispatch")
        self.display_thread = threading.Thread(
            target=self._display_loop, daemon=True, name="google-translate-display")
        self.sync_thread.start()
        self.translate_thread.start()
        self.display_thread.start()

    @classmethod
    def _last_eos(cls, text):
        return max((text.rfind(ch) for ch in cls.PUNC_EOS), default=-1)

    @classmethod
    def _extract_original_caption(cls, full_text):
        """Mirror Translator.SyncLoop's latestCaption -> OriginalCaption extraction."""
        if not full_text:
            return ""

        if full_text[-1] in cls.PUNC_EOS:
            last_eos_index = cls._last_eos(full_text[:-1])
        else:
            last_eos_index = cls._last_eos(full_text)

        latest_caption = full_text[last_eos_index + 1:]

        # LiveCaptions may emit EOS together with only a tiny following fragment.
        # Upstream extends backward by one sentence in that case.
        if last_eos_index > 0 and len(latest_caption.encode("utf-8")) < cls.SHORT_THRESHOLD:
            previous_eos = cls._last_eos(full_text[:last_eos_index])
            latest_caption = full_text[previous_eos + 1:]

        # OriginalCaption keeps only the complete portion when the extracted
        # latest caption contains an EOS.
        last_eos = cls._last_eos(latest_caption)
        if last_eos != -1:
            latest_caption = latest_caption[:last_eos + 1]
        return latest_caption

    def _observe(self, text):
        with self.state_lock:
            self.observed_text = text or ""

    def partial(self, text):
        self._observe(text)

    def final(self, text, a=None, b=None):
        # Upstream has no separate "final event": both partial and final text
        # simply update the currently observed caption. Translation timing is
        # decided only by the SyncLoop-equivalent state machine below.
        self._observe(text)

    def _sync_once(self):
        """One 25 ms SyncLoop-equivalent step. Returns enqueued text or None."""
        with self.state_lock:
            full_text = self.observed_text
            if not full_text:
                return None

            latest_caption = self._extract_original_caption(full_text)
            enqueued = None

            if self.original_caption != latest_caption:
                self.original_caption = latest_caption
                self.idle_count = 0

                if self.original_caption and self.original_caption[-1] in self.PUNC_EOS:
                    self.sync_count = 0
                    enqueued = self.original_caption
                elif len(self.original_caption.encode("utf-8")) >= self.SHORT_THRESHOLD:
                    self.sync_count += 1
            else:
                self.idle_count += 1

            if (self.sync_count > self.MAX_SYNC_INTERVAL or
                    self.idle_count == self.MAX_IDLE_INTERVAL):
                self.sync_count = 0
                enqueued = self.original_caption

        if enqueued:
            self.pending_text_queue.put(enqueued)
        return enqueued

    def _sync_loop(self):
        while not self.closed:
            self._sync_once()
            time.sleep(self.SYNC_TICK)

    def _translate_loop(self):
        while not self.closed:
            try:
                original_snapshot = self.pending_text_queue.get_nowait()
            except queue.Empty:
                original_snapshot = None

            if original_snapshot:
                self._enqueue_translation_task(original_snapshot)
            time.sleep(self.TRANSLATE_TICK)

    def _enqueue_translation_task(self, original_text):
        with self.task_lock:
            self.next_task_id += 1
            task = {
                "id": self.next_task_id,
                "text": original_text,
                "cancelled": False,
            }
            self.tasks.append(task)

        th = threading.Thread(
            target=self._translation_worker,
            args=(task,),
            daemon=True,
            name=f"google-translate-task-{task['id']}",
        )
        task["thread"] = th
        th.start()
        return task

    def _translation_worker(self, task):
        original_text = task["text"]
        is_choke = bool(original_text and original_text[-1] in self.PUNC_EOS)
        try:
            translated = google_translate(original_text, self.target)
        except Exception as e:
            # Upstream Translate() converts non-cancellation failures into a
            # normal translated result carrying an [ERROR] prefix.
            translated = f"[ERROR] Translation Failed: {e}"

        with self.task_lock:
            if task.get("cancelled") or task not in self.tasks:
                return

        self._on_task_completed(task, translated, is_choke)

    def _on_task_completed(self, completed_task, translated, is_choke):
        """Mirror TranslationTaskQueue.OnTaskCompleted ordering semantics."""
        with self.task_lock:
            if completed_task not in self.tasks:
                return False

            index = self.tasks.index(completed_task)

            # A newer completed task cancels/removes every older task. urllib
            # cannot abort an in-flight request, so cancelled workers are
            # ignored when they eventually return.
            for older in self.tasks[:index]:
                older["cancelled"] = True
            del self.tasks[:index + 1]

        with self.output_lock:
            self.output = (completed_task["text"], translated, is_choke)
            self.output_version += 1
        return True

    def _display_loop(self):
        seen_version = 0
        while not self.closed:
            with self.output_lock:
                version = self.output_version
                output = self.output

            if version != seen_version:
                seen_version = version
                source, translated, is_choke = output
                self.overlay.translation(source, translated)
                if is_choke:
                    time.sleep(self.COMPLETE_SENTENCE_CHOKE)

            time.sleep(self.DISPLAY_TICK)

    def close(self):
        self.closed = True
        for th in (self.sync_thread, self.translate_thread, self.display_thread):
            th.join(timeout=0.5)


class MultiSink:
    def __init__(self, *sinks):
        self.sinks = sinks

    def partial(self, text):
        for s in self.sinks:
            s.partial(text)

    def final(self, text, a, b):
        for s in self.sinks:
            s.final(text, a, b)


# ============================ 识别后端 ============================
QWEN_LANG = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "yue": "Cantonese",
             "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian", "pt": "Portuguese",
             "it": "Italian", "ar": "Arabic", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian"}


class LlamaBackend:
    """llama.cpp llama-server (b10941+ 支持 Qwen3-ASR 的 mtmd 音频)。"""
    name = "llama.cpp"

    def __init__(self, server_exe, model, mmproj, lang, hotwords, use_context, port=8765, url=None, slots=2):
        import subprocess, urllib.request, socket, json
        self.urllib = urllib.request
        self.lang = QWEN_LANG.get(lang, lang) if lang else None
        self.hot = hotwords or ""
        self.use_context = use_context
        self.last = ""
        self.proc = None
        self.job = None
        if url is None:
            # 端口上已经有一个跑着同一个模型的 server（上次没关干净）就直接复用；被别的东西占着就换端口
            existing = self._probe(f"http://127.0.0.1:{port}", model)
            if existing == "same":
                self.url = f"http://127.0.0.1:{port}"
                print(f"复用已在运行的 llama-server :{port}")
            else:
                if existing == "other":
                    with socket.socket() as sk:
                        sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
                self.url = f"http://127.0.0.1:{port}"
                cmd = [server_exe, "-m", model, "--mmproj", mmproj, "-ngl", "99", "--port", str(port),
                       "-c", "8192", "--no-webui", "-np", str(slots), "--log-disable"]
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._bind_job(self.proc)
        else:
            self.url = url
        # 等 /health
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                with self.urllib.urlopen(self.url + "/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                if self.proc is not None and self.proc.poll() is not None:
                    raise RuntimeError("llama-server 启动失败（退出码 %s）" % self.proc.returncode)
                time.sleep(0.5)
        else:
            raise RuntimeError("llama-server 没起来")

    def _probe(self, url, model):
        """返回 'none'(端口空闲) / 'same'(同模型 server 在跑) / 'other'(端口被占但不是我们的)"""
        import json, socket
        with socket.socket() as sk:
            sk.settimeout(0.3)
            if sk.connect_ex(("127.0.0.1", int(url.rsplit(":", 1)[1]))) != 0:
                return "none"
        try:
            with self.urllib.urlopen(url + "/props", timeout=2) as r:
                props = json.loads(r.read().decode())
            if os.path.basename(props.get("model_path", "")) == os.path.basename(model):
                return "same"
        except Exception:
            pass
        return "other"

    @staticmethod
    def _bind_job(proc):
        """把 server 放进一个 Job Object：本进程一退出（包括崩溃/被杀），server 跟着被系统结束。"""
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                              "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]
            job = k32.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
            k32.AssignProcessToJobObject(job, wintypes.HANDLE(proc._handle))
            LlamaBackend._job_handle = job   # 保持引用，进程退出时句柄关闭 -> server 被杀
        except Exception as e:
            print("Job Object 绑定失败（server 可能不会随程序退出）:", e)

    def _prompt(self):
        ctx = self.hot
        if self.use_context and self.last:
            ctx = (ctx + "\n" + self.last[-80:]).strip()
        p = ("<|im_start|>system\n" + ctx + "<|im_end|>\n"
             "<|im_start|>user\n<|audio_start|><__media__><|audio_end|><|im_end|>\n"
             "<|im_start|>assistant\n")
        if self.lang:
            p += f"language {self.lang}<asr_text>"
        return p

    @staticmethod
    def _wav_b64(audio):
        import io, wave, base64
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        return base64.b64encode(b.getvalue()).decode()

    def warmup(self):
        self.transcribe(np.zeros(SR, dtype=np.float32))

    def transcribe(self, audio):
        import json
        ctx = self.hot
        if self.use_context and self.last:
            ctx = (ctx + "\n" + self.last[-80:]).strip()
        msgs = [{"role": "system", "content": ctx},
                {"role": "user", "content": [{"type": "input_audio",
                                              "input_audio": {"data": self._wav_b64(audio), "format": "wav"}}]}]
        body = json.dumps({"messages": msgs, "max_tokens": 200, "temperature": 0.0, "cache_prompt": True}).encode()
        req = self.urllib.Request(self.url + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
        with self.urllib.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        text = out["choices"][0]["message"]["content"]
        # 模型输出形如 "language Chinese<asr_text>正文"
        if "<asr_text>" in text:
            lang, text = text.split("<asr_text>", 1)
            if self.lang and self.lang not in lang:
                return ""      # 指定了语言，但这段判成别的语言（多半是音乐/噪声），丢掉
        return _clean(text)

    def close(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass


class DebugLog:
    """--debug DIR：每个最终段存 wav + 一行 jsonl（长度/时延/文本），用来调参。"""

    def __init__(self, d):
        import json
        self.json = json
        self.d = d
        os.makedirs(d, exist_ok=True)
        self.f = open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8")
        self.t0 = time.time()

    def write(self, rec, audio=None):
        import wave
        if audio is not None:
            fn = f"seg{rec['seg']:04d}.wav"
            with wave.open(os.path.join(self.d, fn), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            rec["wav"] = fn
        self.f.write(self.json.dumps(rec, ensure_ascii=False) + "\n"); self.f.flush()


def find_llama_server(here):
    import shutil
    cands = [os.environ.get("LLAMA_SERVER"), os.path.join(here, "llama", "llama-server.exe"),
             os.path.join(here, "llama-server.exe"), shutil.which("llama-server")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError("找不到 llama-server.exe：请把 llama.cpp 的 Windows CUDA 构建解压到脚本旁的 llama/ 目录，"
                       "或设置环境变量 LLAMA_SERVER，或用 --llama-server 指定")


def load_hotwords(args):
    words = []
    path = args.hotwords_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotwords.txt")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#")[0].strip()
            if line:
                words += [w.strip() for w in re.split(r"[,，\s]+", line) if w.strip()]
    if args.hotwords:
        words += [w.strip() for w in args.hotwords.split(",") if w.strip()]
    return ",".join(dict.fromkeys(words))


# ============================ 识别循环 ============================
def _clean(text):
    text = re.sub(r"(.)\1{4,}", r"\1", text.strip())        # "语语语语语" 复读幻觉
    text = re.sub(r"(.{2,6})(?:\1){3,}", r"\1", text)        # 短语复读
    text = re.sub(r"\b([A-Za-z0-9])((?: [A-Za-z0-9])+)\b", lambda m: m.group(0).replace(" ", ""), text)  # "B L G" -> "BLG"
    text = re.sub(r"(?<=[一-鿿]) (?=[A-Za-z0-9])|(?<=[A-Za-z0-9]) (?=[一-鿿])", "", text)  # 中英之间去空格
    return text


def run_asr(args, sink, stop, status=None):
    from faster_whisper.vad import get_speech_timestamps, VadOptions, get_vad_model
    import collections

    pa = pyaudio.PyAudio()
    dev = get_loopback_device(pa, args.device)
    print(f"采集: {dev['name']}  ({int(dev['defaultSampleRate'])} Hz, {int(dev['maxInputChannels'])} ch)")
    hot = load_hotwords(args)
    if hot:
        print(f"热词: {hot}")
    here = os.path.dirname(os.path.abspath(__file__))
    t = time.time()
    if not args.llama_server:
        args.llama_server = find_llama_server(here)
    model = args.model or os.path.join(here, "models", "Qwen3-ASR-1.7B-Q8_0.gguf")
    mmproj = args.mmproj or os.path.join(here, "models", "mmproj-Qwen3-ASR-1.7B-bf16.gguf")
    print(f"加载模型 {os.path.basename(model)} (llama.cpp) …", flush=True)
    if status:
        status("加载模型 (llama.cpp) …")
    be = LlamaBackend(args.llama_server, model, mmproj, args.lang, hot, args.context, args.llama_port, args.llama_url, args.slots)
    get_vad_model()
    be.warmup()
    print(f"模型就绪 {time.time() - t:.1f}s。开始监听。\n", flush=True)
    if status:
        status("● 监听中")
    dbg = DebugLog(args.debug) if args.debug else None

    # ---- 两个识别线程：A 只做最终句(按序)，B 只做临时句(只留最新)；server 开了 2 个槽位可并行 ----
    cv = threading.Condition()
    final_q = collections.deque()
    partial_slot = [None]
    last_final_seg = [-1]
    partial_done = {}          # seg -> (覆盖到的样本数, 文本)，句尾若无新语音则直接复用，省一次推理

    def log_dbg(job, text, t0, t1, reused=False):
        if not dbg:
            return
        rec = {"seg": job["seg"], "kind": job["kind"], "reason": job.get("reason"), "reused": reused,
               "audio_sec": round(len(job["audio"]) / SR, 2),
               "speech_end": round(job["t_end"] - dbg.t0, 2), "queued": round(job["t_q"] - dbg.t0, 2),
               "infer_start": round(t0 - dbg.t0, 2), "infer_sec": round(t1 - t0, 2),
               "latency": round(t1 - job["t_end"], 2), "text": text}
        dbg.write(rec, job["audio"] if job["kind"] == "final" else None)

    def worker_final():
        while True:
            with cv:
                while not final_q and not stop.is_set():
                    cv.wait(0.2)
                if not final_q:
                    return
                job = final_q.popleft()
            t0 = time.time()
            reused = job.get("text") is not None
            if reused:
                text = job["text"]
            else:
                try:
                    text = be.transcribe(job["audio"])
                except Exception as e:
                    print("识别出错:", repr(e), flush=True); continue
            t1 = time.time()
            last_final_seg[0] = job["seg"]
            if text:
                be.last = text
                sink.final(text, job["t_start"], job["t_end"])
            log_dbg(job, text, t0, t1, reused)

    def worker_partial():
        while not stop.is_set():
            with cv:
                while partial_slot[0] is None and not stop.is_set():
                    cv.wait(0.2)
                if stop.is_set():
                    return
                job, partial_slot[0] = partial_slot[0], None
            t0 = time.time()
            try:
                text = be.transcribe(job["audio"])
            except Exception as e:
                print("识别出错:", repr(e), flush=True); continue
            t1 = time.time()
            if job["seg"] <= last_final_seg[0]:
                continue                          # 这段已经出最终结果了，临时结果作废
            partial_done[job["seg"]] = (len(job["audio"]), text)
            if text:
                sink.partial(text)
            log_dbg(job, text, t0, t1)

    ths = [threading.Thread(target=worker_final, daemon=True), threading.Thread(target=worker_partial, daemon=True)]
    for th in ths:
        th.start()

    def submit(kind, audio, seg, t_start, t_end, reason=None, text=None):
        job = {"kind": kind, "audio": audio, "seg": seg, "t_start": t_start, "t_end": t_end,
               "t_q": time.time(), "reason": reason, "text": text}
        with cv:
            if kind == "final":
                final_q.append(job)
            else:
                partial_slot[0] = job
            cv.notify_all()

    vad_opts = VadOptions(threshold=0.45, min_speech_duration_ms=100, min_silence_duration_ms=200, speech_pad_ms=100)
    audio_q = queue.Queue()
    stream = start_capture(pa, dev, audio_q, stop)

    buf = np.zeros(0, dtype=np.float32)
    seg_id = 0
    seg_start = None
    last_speech = None
    last_partial = 0.0
    vad_win = np.zeros(0, dtype=np.float32)
    WIN = int(SR * args.vad_win)
    speech_samples = 0        # 当前句里最后一个语音窗口结束时的样本数
    run_peak = 0.0     # 自动增益：loopback 是系统音量后的信号，音量小时峰值可能只有 0.01
    t_run0 = time.time()

    try:
        while not stop.is_set():
            if args.duration and time.time() - t_run0 > args.duration:
                break
            try:
                chunk = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.time()
            pk = float(np.abs(chunk).max())
            run_peak = max(pk, run_peak * 0.998)
            if run_peak > 1e-4:
                chunk = chunk * min(args.max_gain, 0.5 / run_peak)
            vad_win = np.concatenate([vad_win, chunk])
            if len(vad_win) < WIN:
                continue
            win, vad_win = vad_win[:WIN], vad_win[WIN:]

            speech = bool(get_speech_timestamps(win, vad_opts)) if np.abs(win).max() > 1e-3 else False

            if speech:
                if seg_start is None:
                    seg_start, last_partial = now - 0.3, now
                last_speech = now
                buf = np.concatenate([buf, win])
                speech_samples = len(buf)
            elif seg_start is not None:
                buf = np.concatenate([buf, win])

            if seg_start is None:
                continue

            seg_len = len(buf) / SR
            sil = now - last_speech
            # 句子结束：静音够长且句子不太短；或静音很长；或句子太长强制切
            reason = None
            if (sil >= args.silence and seg_len >= args.min_seg) or sil >= args.silence * 2.5:
                reason = "silence"
            elif seg_len >= args.max_seg:
                reason = "maxlen"
            if reason:
                done = partial_done.pop(seg_id, None)
                reuse = done[1] if (done and reason == "silence" and done[0] >= speech_samples) else None
                submit("final", buf, seg_id, seg_start, last_speech, reason, reuse)
                seg_id += 1
                buf = np.zeros(0, dtype=np.float32)
                seg_start = last_speech = None
            elif not args.no_partial and now - last_partial >= args.partial_every and seg_len >= 1.0:
                last_partial = now
                submit("partial", buf.copy(), seg_id, seg_start, last_speech)
    finally:
        partial_done.clear()
        stop.set()
        try:
            stream.stop_stream(); stream.close()
        except Exception:
            pass
        pa.terminate()
        if len(buf) > SR * 0.5:
            text = be.transcribe(buf)
            if text:
                sink.final(text, seg_start or time.time(), time.time())
        for th in ths:
            th.join(timeout=10)
        if hasattr(be, "close"):
            be.close()


# ============================ 入口 ============================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="列出输出设备后退出")
    ap.add_argument("--device", type=int, default=None, help="输出设备 index（默认=系统默认输出）")
    ap.add_argument("--model", default=None, help="GGUF 模型路径（默认 models/Qwen3-ASR-1.7B-Q8_0.gguf）")
    ap.add_argument("--mmproj", default=None, help="mmproj GGUF 路径（默认 models/mmproj-Qwen3-ASR-1.7B-bf16.gguf）")
    ap.add_argument("--llama-server", default=None, help="llama-server.exe 路径（默认按 LLAMA_SERVER 环境变量 / 脚本旁 llama/ 目录 / PATH 查找）")
    ap.add_argument("--llama-port", type=int, default=8765)
    ap.add_argument("--llama-url", default=None, help="连接已经在跑的 llama-server，而不是自己起一个")
    ap.add_argument("--debug", default=None, help="目录：留存每段音频 wav + log.jsonl（时延/文本）供分析")
    ap.add_argument("--lang", default=None, help="语言代码 zh/en/ja…，不填=自动检测")
    ap.add_argument("--hotwords", default=None, help="逗号分隔的专有名词，如 'BLG,T1,峡谷先锋'")
    ap.add_argument("--hotwords-file", default=None, help="热词文件（默认读脚本旁边的 hotwords.txt，每行/逗号分隔，# 注释）")
    ap.add_argument("--no-context", dest="context", action="store_false", help="不把上一句喂给模型当上下文")
    ap.add_argument("--partial-every", type=float, default=0.4, help="说话中多少秒刷新一次临时结果")
    ap.add_argument("--silence", type=float, default=0.5, help="静音多少秒算一句结束")
    ap.add_argument("--vad-win", type=float, default=0.2, help="VAD 判定窗口秒数（越小句尾反应越快）")
    ap.add_argument("--slots", type=int, default=2, help="llama-server 并行槽位数")
    ap.add_argument("--min-seg", type=float, default=1.0, help="短于这个秒数的句子先不切，等下一段一起")
    ap.add_argument("--max-seg", type=float, default=6.0, help="一句最长多少秒强制切分（一行字幕 20~30 字）")
    ap.add_argument("--max-gain", type=float, default=60.0, help="自动增益上限倍数")
    ap.add_argument("--out", default=None, help="追加写入 txt")
    ap.add_argument("--srt", default=None, help="追加写入 srt")
    ap.add_argument("--no-partial", action="store_true", help="只输出最终结果")
    ap.add_argument("--console", action="store_true", help="不开悬浮字幕窗，只在终端显示")
    ap.add_argument("--font-size", type=int, default=30, help="字幕字号")
    ap.add_argument("--font", default="Microsoft YaHei")
    ap.add_argument("--width", type=float, default=0.8, help="字幕条宽度占屏幕比例")
    ap.add_argument("--bottom", type=int, default=70, help="字幕条距屏幕底部像素")
    ap.add_argument("--alpha", type=float, default=0.82, help="字幕条不透明度 0~1")
    ap.add_argument("--hold", type=float, default=6.0, help="没新话时字幕保留几秒后清空")
    ap.add_argument("--duration", type=float, default=None, help="运行多少秒后自动退出（测试用）")
    args = ap.parse_args()

    if args.list:
        pa = pyaudio.PyAudio(); list_devices(pa); pa.terminate(); return

    stop = threading.Event()
    console = ConsoleSink(args.out, args.srt, show_partial=args.console and not args.no_partial)

    if args.console:
        try:
            run_asr(args, console, stop)
        except KeyboardInterrupt:
            stop.set()
        console.close()
        print("\n已退出。")
        return

    overlay = OverlaySink(args.font_size, args.width, args.bottom, args.alpha, 2, args.hold, args.font)
    translator = GoogleTranslateSink(overlay, target="zh-CN")
    sink = MultiSink(console, overlay, translator)

    def worker():
        try:
            run_asr(args, sink, stop, status=overlay.status)
        except Exception as e:
            print("识别线程出错:", repr(e), flush=True)
        finally:
            overlay.request_close()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    stop.set()
    th.join(timeout=5)
    translator.close()
    console.close()
    print("\n已退出。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_caption.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{tb}")
        print(tb)
        print("已写入 live_caption.log，按回车退出"); input()