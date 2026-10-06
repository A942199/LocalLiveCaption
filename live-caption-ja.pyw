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
字幕窗: 鼠标拖动移动位置；滚轮调字号；右上角 ⚙ 打开设置；× 或 Esc 退出。终端 Ctrl+C 退出。
"""
import argparse
import os
import re
import sys
import time
import threading
import queue
import json
import collections
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
        self.settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live-caption-settings.json")
        self._initial_settings = {
            "asr_model": "Q8_0",
            "context_enabled": True,
            "context_history": 1,
            "context_chars": 80,
            "llama_ctx": 8192,
            "slots": 2,
            "max_tokens": 200,
            "temperature": 0.0,
            "partial_every": 0.4,
            "stable_count": 3,
            "punct_silence": 0.40,
            "hesitation_silence": 1.10,
            "silence": 0.5,
            "vad_win": 0.2,
            "vad_threshold": 0.45,
            "vad_min_speech_ms": 100,
            "vad_min_silence_ms": 200,
            "vad_speech_pad_ms": 100,
            "min_seg": 1.0,
            "max_seg": 6.0,
            "max_gain": 60.0,
            "font_name": font,
            "font_size": font_size,
            "alpha": alpha,
            "width_frac": width_frac,
            "window_height": 0,
            "bottom": bottom,
            "hold": hold,
            "current_color": "#ffffff",
            "translation_color": "#d0d0d0",
            "previous_color": "#9a9a9a",
            "background_color": "#000000",
            "topmost": True,
            "show_previous": True,
            "history_count": 1,
            "current_bold": True,
        }
        cfg = self._load_settings(self._initial_settings)

        def number(key, cast, low, high):
            try:
                value = cast(cfg[key])
            except (TypeError, ValueError):
                value = cast(self._initial_settings[key])
            return max(low, min(high, value))

        def flag(key):
            value = cfg[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(value)

        font_value = cfg["font_name"]
        model_value = str(cfg.get("asr_model", "Q8_0")).upper()
        self.asr_model = model_value if model_value in ("Q8_0", "BF16") else "Q8_0"
        self.context_enabled = flag("context_enabled")
        self.context_history = number("context_history", int, 1, 5)
        self.context_chars = number("context_chars", int, 40, 1000)
        self.llama_ctx = number("llama_ctx", int, 2048, 16384)
        self.slots = number("slots", int, 1, 4)
        self.max_tokens = number("max_tokens", int, 64, 512)
        self.temperature = number("temperature", float, 0.0, 1.0)
        self.partial_every = number("partial_every", float, 0.2, 2.0)
        self.stable_count = number("stable_count", int, 2, 5)
        self.punct_silence = number("punct_silence", float, 0.2, 1.5)
        self.hesitation_silence = number("hesitation_silence", float, 0.5, 2.5)
        self.silence = number("silence", float, 0.2, 2.0)
        self.vad_win = number("vad_win", float, 0.1, 1.0)
        self.vad_threshold = number("vad_threshold", float, 0.1, 0.9)
        self.vad_min_speech_ms = number("vad_min_speech_ms", int, 50, 1000)
        self.vad_min_silence_ms = number("vad_min_silence_ms", int, 50, 2000)
        self.vad_speech_pad_ms = number("vad_speech_pad_ms", int, 0, 1000)
        self.min_seg = number("min_seg", float, 0.2, 5.0)
        self.max_seg = number("max_seg", float, 2.0, 30.0)
        self.max_gain = number("max_gain", float, 1.0, 100.0)
        self.font_name = font_value.strip() if isinstance(font_value, str) and font_value.strip() else str(font)
        self.font_size = number("font_size", int, 12, 80)
        self.alpha = number("alpha", float, 0.20, 1.0)
        self.width_frac = number("width_frac", float, 0.30, 1.0)
        self.window_height = number("window_height", int, 0, 10000)
        self.bottom = number("bottom", int, 0, 500)
        self.hold = number("hold", float, 1.0, 20.0)
        self.current_color = str(cfg["current_color"])
        self.translation_color = str(cfg["translation_color"])
        self.previous_color = str(cfg["previous_color"])
        self.background_color = str(cfg["background_color"])
        self.topmost = flag("topmost")
        self.show_previous = flag("show_previous")
        self.history_count = number("history_count", int, 0, 5)
        self.current_bold = flag("current_bold")
        self.settings_win = None
        self.asr_settings_win = None
        self._setting_vars = None
        self._settings_save_job = None
        self.finals = []          # 当前 final + 可配置数量的历史 final
        self.partial_text = ""
        self.translation_text = ""
        self.translation_source = ""
        self.caption_seen = False    # 启动状态条在首句字幕出现前保持可见
        self.last_update = time.time()
        self.fading = False
        self.closed = False

        root = tk.Tk()
        self.root = root
        for attr, fallback in (
            ("current_color", "#ffffff"),
            ("translation_color", "#d0d0d0"),
            ("previous_color", "#9a9a9a"),
            ("background_color", "#000000"),
        ):
            setattr(self, attr, self._safe_color(getattr(self, attr), fallback))
        root.overrideredirect(True)
        root.attributes("-topmost", self.topmost)
        root.attributes("-alpha", self.alpha)
        root.configure(bg=self.background_color)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.w = int(sw * self.width_frac)
        self.sh = sh

        self.f_cur = tkfont.Font(family=self.font_name, size=self.font_size,
                                 weight="bold" if self.current_bold else "normal")
        self.f_trans = tkfont.Font(family=self.font_name, size=max(12, int(self.font_size * 0.82)), weight="normal")
        self.f_prev = tkfont.Font(family=self.font_name, size=max(10, int(self.font_size * 0.68)), weight="normal")
        self.l_prev = tk.Label(root, text="", fg=self.previous_color, bg=self.background_color, justify="center", anchor="s",
                               font=self.f_prev, wraplength=self.w - 40, padx=20)
        self.l_cur = tk.Label(root, text="", fg=self.current_color, bg=self.background_color, justify="center", anchor="n",
                              font=self.f_cur, wraplength=self.w - 40, padx=20)
        self.l_trans = tk.Label(root, text="", fg=self.translation_color, bg=self.background_color, justify="center", anchor="n",
                                font=self.f_trans, wraplength=self.w - 40, padx=20)
        if self.show_previous:
            self.l_prev.pack(side="top", fill="x", pady=(8, 0))
        self.l_cur.pack(side="top", fill="x", pady=(2, 0))
        self.l_trans.pack(side="top", fill="x", pady=(0, 10))
        self.hint = tk.Label(root, text="拖动=移动  边缘/角=缩放  滚轮=字号  ⚙=设置  Esc=退出",
                             fg="#777777", bg=self.background_color, font=(self.font_name, 9))
        self.hint.place(relx=1.0, rely=0.0, x=-86, anchor="ne")
        self.window_controls = tk.Frame(root, bg=self.background_color, bd=0, highlightthickness=0)
        self.window_controls.place(relx=1.0, rely=0.0, x=-6, y=2, anchor="ne")
        self.settings_btn = tk.Button(self.window_controls, text="⚙", command=self.open_settings,
                                      fg="#b0b0b0", bg=self.background_color,
                                      activeforeground="#ffffff", activebackground="#333333", relief="flat",
                                      bd=0, highlightthickness=0, padx=6, pady=0,
                                      font=("Segoe UI Symbol", 11), cursor="hand2")
        self.settings_btn.pack(side="left", padx=(0, 8))
        self.close_btn = tk.Button(self.window_controls, text="×", command=self.close,
                                   fg="#b0b0b0", bg=self.background_color,
                                   activeforeground="#ffffff", activebackground="#333333", relief="flat",
                                   bd=0, highlightthickness=0, padx=6, pady=0,
                                   font=("Segoe UI", 13, "bold"), cursor="hand2")
        self.close_btn.pack(side="left")
        root.after(5000, self.hint.place_forget)
        self._relayout(first=True)

        for wdg in (root, self.l_prev, self.l_cur, self.l_trans):
            wdg.bind("<Motion>", self._resize_cursor)
            wdg.bind("<ButtonPress-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
            wdg.bind("<ButtonRelease-1>", self._drag_end)
            wdg.bind("<MouseWheel>", self._wheel)
        root.bind("<Escape>", lambda e: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(60, self._poll)

    def _load_settings(self, defaults):
        cfg = dict(defaults)
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for key in defaults:
                    if key in saved:
                        cfg[key] = saved[key]
        except (OSError, ValueError, TypeError):
            pass
        return cfg

    def _safe_color(self, color, fallback):
        try:
            self.root.winfo_rgb(color)
            return color
        except Exception:
            return fallback

    def _current_settings(self):
        return {
            "asr_model": self.asr_model,
            "context_enabled": self.context_enabled,
            "context_history": self.context_history,
            "context_chars": self.context_chars,
            "llama_ctx": self.llama_ctx,
            "slots": self.slots,
            "max_tokens": self.max_tokens,
            "temperature": round(self.temperature, 3),
            "partial_every": round(self.partial_every, 3),
            "stable_count": self.stable_count,
            "punct_silence": round(self.punct_silence, 3),
            "hesitation_silence": round(self.hesitation_silence, 3),
            "silence": round(self.silence, 3),
            "vad_win": round(self.vad_win, 3),
            "vad_threshold": round(self.vad_threshold, 3),
            "vad_min_speech_ms": self.vad_min_speech_ms,
            "vad_min_silence_ms": self.vad_min_silence_ms,
            "vad_speech_pad_ms": self.vad_speech_pad_ms,
            "min_seg": round(self.min_seg, 3),
            "max_seg": round(self.max_seg, 3),
            "max_gain": round(self.max_gain, 2),
            "font_name": self.font_name,
            "font_size": self.font_size,
            "alpha": round(self.alpha, 3),
            "width_frac": round(self.width_frac, 3),
            "window_height": int(self.window_height),
            "bottom": self.bottom,
            "hold": round(self.hold, 2),
            "current_color": self.current_color,
            "translation_color": self.translation_color,
            "previous_color": self.previous_color,
            "background_color": self.background_color,
            "topmost": self.topmost,
            "show_previous": self.show_previous,
            "history_count": self.history_count,
            "current_bold": self.current_bold,
        }

    def _save_settings(self):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self._current_settings(), f, ensure_ascii=False, indent=2)
        except OSError as e:
            print("保存字幕设置失败:", repr(e), flush=True)

    def _schedule_save(self):
        if self._settings_save_job is not None:
            try:
                self.root.after_cancel(self._settings_save_job)
            except Exception:
                pass
        self._settings_save_job = self.root.after(350, self._save_settings)

    def _apply_style(self, anchor_to_screen=False, keep_center=False):
        old_center = self.root.winfo_x() + self.root.winfo_width() / 2
        self.root.configure(bg=self.background_color)
        self.root.attributes("-topmost", self.topmost)
        if not self.fading:
            self.root.attributes("-alpha", self.alpha)

        self.f_cur.configure(family=self.font_name, size=self.font_size,
                             weight="bold" if self.current_bold else "normal")
        self.f_trans.configure(family=self.font_name, size=max(12, int(self.font_size * 0.82)))
        self.f_prev.configure(family=self.font_name, size=max(10, int(self.font_size * 0.68)))
        self.hint.configure(bg=self.background_color, font=(self.font_name, 9))
        self.window_controls.configure(bg=self.background_color)
        self.settings_btn.configure(bg=self.background_color)
        self.close_btn.configure(bg=self.background_color)

        sw = self.root.winfo_screenwidth()
        self.w = max(320, int(sw * self.width_frac))
        wrap = max(280, self.w - 40)
        self.l_prev.configure(fg=self.previous_color, bg=self.background_color, wraplength=wrap)
        self.l_cur.configure(fg=self.current_color, bg=self.background_color, wraplength=wrap)
        self.l_trans.configure(fg=self.translation_color, bg=self.background_color, wraplength=wrap)

        for label in (self.l_prev, self.l_cur, self.l_trans):
            label.pack_forget()
        if self.show_previous:
            self.l_prev.pack(side="top", fill="x", pady=(8, 0))
        self.l_cur.pack(side="top", fill="x", pady=(2, 0))
        self.l_trans.pack(side="top", fill="x", pady=(0, 10))

        self._relayout(anchor_to_screen=anchor_to_screen, center_x=old_center if keep_center else None)
        self._render()

    def _settings_changed(self, key=None):
        if not self._setting_vars:
            return
        v = self._setting_vars
        if key == "asr_model":
            value = str(v["asr_model"].get()).upper()
            self.asr_model = value if value in ("Q8_0", "BF16") else "Q8_0"
            self._schedule_save()
            return
        recognition_keys = {
            "context_enabled", "context_history", "context_chars", "llama_ctx", "slots",
            "max_tokens", "temperature", "partial_every", "stable_count",
            "punct_silence", "hesitation_silence", "silence", "vad_win",
            "vad_threshold", "vad_min_speech_ms", "vad_min_silence_ms", "vad_speech_pad_ms",
            "min_seg", "max_seg", "max_gain",
        }
        if key in recognition_keys:
            try:
                self.context_enabled = bool(v["context_enabled"].get())
                self.context_history = max(1, min(5, int(v["context_history"].get())))
                self.context_chars = max(40, min(1000, int(v["context_chars"].get())))
                self.llama_ctx = max(2048, min(16384, int(v["llama_ctx"].get())))
                self.slots = max(1, min(4, int(v["slots"].get())))
                self.max_tokens = max(64, min(512, int(v["max_tokens"].get())))
                self.temperature = max(0.0, min(1.0, float(v["temperature"].get())))
                self.partial_every = max(0.2, min(2.0, float(v["partial_every"].get())))
                self.stable_count = max(2, min(5, int(v["stable_count"].get())))
                self.punct_silence = max(0.2, min(1.5, float(v["punct_silence"].get())))
                self.hesitation_silence = max(0.5, min(2.5, float(v["hesitation_silence"].get())))
                self.silence = max(0.2, min(2.0, float(v["silence"].get())))
                self.vad_win = max(0.1, min(1.0, float(v["vad_win"].get())))
                self.vad_threshold = max(0.1, min(0.9, float(v["vad_threshold"].get())))
                self.vad_min_speech_ms = max(50, min(1000, int(v["vad_min_speech_ms"].get())))
                self.vad_min_silence_ms = max(50, min(2000, int(v["vad_min_silence_ms"].get())))
                self.vad_speech_pad_ms = max(0, min(1000, int(v["vad_speech_pad_ms"].get())))
                self.min_seg = max(0.2, min(5.0, float(v["min_seg"].get())))
                self.max_seg = max(2.0, min(30.0, float(v["max_seg"].get())))
                self.max_gain = max(1.0, min(100.0, float(v["max_gain"].get())))
            except (ValueError, TypeError):
                return
            if self.max_seg < self.min_seg:
                self.max_seg = self.min_seg
                v["max_seg"].set(self.max_seg)
            self._schedule_save()
            return
        try:
            self.font_name = v["font_name"].get().strip() or self._initial_settings["font_name"]
            self.font_size = max(12, min(80, int(round(v["font_size"].get()))))
            self.alpha = max(0.20, min(1.0, float(v["alpha"].get())))
            self.width_frac = max(0.30, min(1.0, float(v["width_frac"].get())))
            self.bottom = max(0, min(500, int(round(v["bottom"].get()))))
            self.hold = max(1.0, min(20.0, float(v["hold"].get())))
            self.topmost = bool(v["topmost"].get())
            self.show_previous = bool(v["show_previous"].get())
            self.history_count = max(0, min(5, int(round(v["history_count"].get()))))
            keep = max(1, self.history_count + 1)
            self.finals = self.finals[-keep:]
            if key == "history_count":
                self.window_height = 0
            self.current_bold = bool(v["current_bold"].get())
        except (ValueError, TypeError):
            return
        self._apply_style(anchor_to_screen=(key == "bottom"), keep_center=(key == "width_frac"))
        self._schedule_save()

    def _choose_color(self, key, title):
        from tkinter import colorchooser
        current = getattr(self, key)
        chosen = colorchooser.askcolor(color=current, parent=self.settings_win, title=title)[1]
        if not chosen:
            return
        setattr(self, key, chosen)
        button = getattr(self, f"_color_btn_{key}", None)
        if button is not None:
            button.configure(bg=chosen)
        self._apply_style()
        self._schedule_save()

    def _reset_settings(self):
        if not self._setting_vars:
            return
        d = self._initial_settings
        for key in ("asr_model", "context_enabled", "context_history", "context_chars", "llama_ctx", "slots",
                    "max_tokens", "temperature", "partial_every", "stable_count",
                    "punct_silence", "hesitation_silence", "silence", "vad_win", "vad_threshold",
                    "vad_min_speech_ms", "vad_min_silence_ms", "vad_speech_pad_ms", "min_seg", "max_seg",
                    "max_gain", "font_name", "font_size", "alpha", "width_frac", "bottom", "hold",
                    "topmost", "show_previous", "history_count", "current_bold"):
            self._setting_vars[key].set(d[key])
        for key in ("current_color", "translation_color", "previous_color", "background_color"):
            setattr(self, key, d[key])
            button = getattr(self, f"_color_btn_{key}", None)
            if button is not None:
                button.configure(bg=d[key])
        self.window_height = 0
        self._settings_changed("partial_every")
        self._settings_changed("asr_model")
        self._settings_changed("bottom")

    def _apply_accuracy_preset(self):
        if not self._setting_vars:
            return
        preset = {
            "asr_model": "Q8_0",
            "context_enabled": False,
            "context_history": 1,
            "context_chars": 80,
            "llama_ctx": 4096,
            "slots": 2,
            "max_tokens": 256,
            "temperature": 0.0,
            "partial_every": 0.6,
            "stable_count": 3,
            "punct_silence": 0.40,
            "hesitation_silence": 1.10,
            "silence": 0.80,
            "vad_win": 0.20,
            "vad_threshold": 0.10,
            "vad_min_speech_ms": 100,
            "vad_min_silence_ms": 100,
            "vad_speech_pad_ms": 200,
            "min_seg": 1.2,
            "max_seg": 12.0,
            "max_gain": 60.0,
        }
        for key, value in preset.items():
            self._setting_vars[key].set(value)
        self._settings_changed("partial_every")
        self._settings_changed("asr_model")

    def _close_asr_settings(self):
        if self.asr_settings_win is not None:
            try:
                self.asr_settings_win.destroy()
            except Exception:
                pass
        self.asr_settings_win = None

    def _open_asr_settings(self):
        from tkinter import ttk
        if not self._setting_vars:
            return
        if self.asr_settings_win is not None:
            try:
                if self.asr_settings_win.winfo_exists():
                    self.asr_settings_win.deiconify()
                    self.asr_settings_win.lift()
                    self.asr_settings_win.focus_force()
                    return
            except Exception:
                pass

        win = self.tk.Toplevel(self.settings_win or self.root)
        self.asr_settings_win = win
        win.title("识别参数")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_asr_settings)
        v = self._setting_vars

        outer = self.tk.Frame(win, padx=12, pady=10)
        outer.pack(fill="both", expand=True)

        def add_field(parent, row, col, label, key, start, end, increment):
            self.tk.Label(parent, text=label).grid(row=row, column=col * 2, sticky="w", padx=(0, 6), pady=4)
            box = self.tk.Spinbox(parent, from_=start, to=end, increment=increment, textvariable=v[key],
                                  width=9, command=lambda k=key: self._settings_changed(k))
            box.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 14), pady=4)
            box.bind("<Return>", lambda e, k=key: self._settings_changed(k))
            box.bind("<FocusOut>", lambda e, k=key: self._settings_changed(k))

        context_frame = ttk.LabelFrame(outer, text="上下文 / llama")
        context_frame.pack(fill="x", pady=(0, 8))
        self.tk.Checkbutton(context_frame, text="启用前文上下文", variable=v["context_enabled"],
                            command=lambda: self._settings_changed("context_enabled")).grid(
                                row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))
        add_field(context_frame, 1, 0, "保留 final 句数", "context_history", 1, 5, 1)
        add_field(context_frame, 1, 1, "最多字符数", "context_chars", 40, 1000, 20)
        add_field(context_frame, 2, 0, "llama ctx 总长度", "llama_ctx", 2048, 16384, 1024)
        add_field(context_frame, 2, 1, "并行 slots", "slots", 1, 4, 1)
        add_field(context_frame, 3, 0, "最大输出 tokens", "max_tokens", 64, 512, 16)
        add_field(context_frame, 3, 1, "temperature", "temperature", 0.0, 1.0, 0.05)

        vad_frame = ttk.LabelFrame(outer, text="VAD")
        vad_frame.pack(fill="x", pady=(0, 8))
        add_field(vad_frame, 0, 0, "阈值", "vad_threshold", 0.1, 0.9, 0.05)
        add_field(vad_frame, 0, 1, "检测窗口 (s)", "vad_win", 0.1, 1.0, 0.05)
        add_field(vad_frame, 1, 0, "最短语音 (ms)", "vad_min_speech_ms", 50, 1000, 50)
        add_field(vad_frame, 1, 1, "最短静音 (ms)", "vad_min_silence_ms", 50, 2000, 50)
        add_field(vad_frame, 2, 0, "语音 padding (ms)", "vad_speech_pad_ms", 0, 1000, 50)

        segment_frame = ttk.LabelFrame(outer, text="切句 / 实时性")
        segment_frame.pack(fill="x", pady=(0, 8))
        add_field(segment_frame, 0, 0, "partial 刷新 (s)", "partial_every", 0.2, 2.0, 0.05)
        add_field(segment_frame, 0, 1, "Stable 连续次数", "stable_count", 2, 5, 1)
        add_field(segment_frame, 1, 0, "普通句尾静音 (s)", "silence", 0.2, 2.0, 0.05)
        add_field(segment_frame, 1, 1, "标点句尾静音 (s)", "punct_silence", 0.2, 1.5, 0.05)
        add_field(segment_frame, 2, 0, "犹豫词静音 (s)", "hesitation_silence", 0.5, 2.5, 0.05)
        add_field(segment_frame, 2, 1, "最短句段 (s)", "min_seg", 0.2, 5.0, 0.1)
        add_field(segment_frame, 3, 0, "最长句段 (s)", "max_seg", 2.0, 30.0, 0.5)
        add_field(segment_frame, 3, 1, "自动增益上限", "max_gain", 1.0, 100.0, 1.0)

        note = self.tk.Label(
            outer,
            text="这些参数会自动保存，并在下一次启动字幕程序时生效。命令行显式参数仍然优先。",
            fg="#666666", anchor="w",
        )
        note.pack(fill="x", pady=(2, 8))
        buttons = self.tk.Frame(outer)
        buttons.pack(fill="x")
        self.tk.Button(buttons, text="高精度推荐", command=self._apply_accuracy_preset, width=12).pack(side="left")
        self.tk.Button(buttons, text="关闭", command=self._close_asr_settings, width=10).pack(side="right")

        win.update_idletasks()
        x = max(20, self.settings_win.winfo_x() - win.winfo_width() - 12) if self.settings_win else 20
        y = max(20, self.settings_win.winfo_y()) if self.settings_win else 20
        win.geometry(f"+{x}+{y}")
        win.lift()

    def _close_settings(self):
        self._save_settings()
        self._close_asr_settings()
        if self.settings_win is not None:
            try:
                self.settings_win.destroy()
            except Exception:
                pass
        self.settings_win = None
        self._setting_vars = None

    def open_settings(self):
        import tkinter.font as tkfont
        from tkinter import ttk

        if self.settings_win is not None:
            try:
                if self.settings_win.winfo_exists():
                    self.settings_win.deiconify()
                    self.settings_win.lift()
                    self.settings_win.focus_force()
                    return
            except Exception:
                pass

        win = self.tk.Toplevel(self.root)
        self.settings_win = win
        win.title("字幕设置")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        v = {
            "asr_model": self.tk.StringVar(value=self.asr_model),
            "context_enabled": self.tk.BooleanVar(value=self.context_enabled),
            "context_history": self.tk.IntVar(value=self.context_history),
            "context_chars": self.tk.IntVar(value=self.context_chars),
            "llama_ctx": self.tk.IntVar(value=self.llama_ctx),
            "slots": self.tk.IntVar(value=self.slots),
            "max_tokens": self.tk.IntVar(value=self.max_tokens),
            "temperature": self.tk.DoubleVar(value=self.temperature),
            "partial_every": self.tk.DoubleVar(value=self.partial_every),
            "stable_count": self.tk.IntVar(value=self.stable_count),
            "punct_silence": self.tk.DoubleVar(value=self.punct_silence),
            "hesitation_silence": self.tk.DoubleVar(value=self.hesitation_silence),
            "silence": self.tk.DoubleVar(value=self.silence),
            "vad_win": self.tk.DoubleVar(value=self.vad_win),
            "vad_threshold": self.tk.DoubleVar(value=self.vad_threshold),
            "vad_min_speech_ms": self.tk.IntVar(value=self.vad_min_speech_ms),
            "vad_min_silence_ms": self.tk.IntVar(value=self.vad_min_silence_ms),
            "vad_speech_pad_ms": self.tk.IntVar(value=self.vad_speech_pad_ms),
            "min_seg": self.tk.DoubleVar(value=self.min_seg),
            "max_seg": self.tk.DoubleVar(value=self.max_seg),
            "max_gain": self.tk.DoubleVar(value=self.max_gain),
            "font_name": self.tk.StringVar(value=self.font_name),
            "font_size": self.tk.DoubleVar(value=self.font_size),
            "alpha": self.tk.DoubleVar(value=self.alpha),
            "width_frac": self.tk.DoubleVar(value=self.width_frac),
            "bottom": self.tk.DoubleVar(value=self.bottom),
            "hold": self.tk.DoubleVar(value=self.hold),
            "topmost": self.tk.BooleanVar(value=self.topmost),
            "show_previous": self.tk.BooleanVar(value=self.show_previous),
            "history_count": self.tk.DoubleVar(value=self.history_count),
            "current_bold": self.tk.BooleanVar(value=self.current_bold),
        }
        self._setting_vars = v

        frame = self.tk.Frame(win, padx=14, pady=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row = 0
        self.tk.Label(frame, text="ASR 主模型").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        model_holder = self.tk.Frame(frame)
        model_holder.grid(row=row, column=1, sticky="ew", pady=5)
        model_box = ttk.Combobox(model_holder, textvariable=v["asr_model"],
                                 values=("Q8_0", "BF16"), width=12, state="readonly")
        model_box.pack(side="left")
        model_box.bind("<<ComboboxSelected>>", lambda e: self._settings_changed("asr_model"))
        models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        q8_ok = os.path.isfile(os.path.join(models_dir, "Qwen3-ASR-1.7B-Q8_0.gguf"))
        bf16_ok = os.path.isfile(os.path.join(models_dir, "Qwen3-ASR-1.7B-bf16.gguf"))
        availability = f"Q8_0 {'✓' if q8_ok else '✗'}   BF16 {'✓' if bf16_ok else '✗'}"
        self.tk.Label(model_holder, text=availability, fg="#666666").pack(side="left", padx=(10, 0))
        row += 1

        self.tk.Label(frame, text="识别参数").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        self.tk.Button(frame, text="VAD / 切句 / 上下文…", command=self._open_asr_settings,
                       width=22).grid(row=row, column=1, sticky="w", pady=5)
        row += 1

        self.tk.Label(frame, text="字体").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        fonts = sorted(set(tkfont.families()))
        font_box = ttk.Combobox(frame, textvariable=v["font_name"], values=fonts, width=29)
        font_box.grid(row=row, column=1, sticky="ew", pady=5)
        font_box.bind("<<ComboboxSelected>>", lambda e: self._settings_changed("font_name"))
        font_box.bind("<Return>", lambda e: self._settings_changed("font_name"))
        row += 1

        def add_scale(label, key, start, end, resolution, display):
            nonlocal row
            self.tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            holder = self.tk.Frame(frame)
            holder.grid(row=row, column=1, sticky="ew", pady=4)
            scale = self.tk.Scale(holder, from_=start, to=end, resolution=resolution, orient="horizontal",
                                  showvalue=False, variable=v[key], length=230,
                                  command=lambda _value, k=key: (value_label.configure(text=display(v[k].get())),
                                                                 self._settings_changed(k)))
            scale.pack(side="left", fill="x", expand=True)
            value_label = self.tk.Label(holder, width=8, anchor="e", text=display(v[key].get()))
            value_label.pack(side="right")
            row += 1

        add_scale("字号", "font_size", 12, 80, 1, lambda x: f"{int(round(x))} pt")
        add_scale("窗口透明度", "alpha", 0.20, 1.00, 0.01, lambda x: f"{int(round(x * 100))}%")
        add_scale("窗口宽度", "width_frac", 0.30, 1.00, 0.01, lambda x: f"{int(round(x * 100))}%")
        add_scale("距屏幕底部", "bottom", 0, 500, 5, lambda x: f"{int(round(x))} px")
        add_scale("字幕停留时间", "hold", 1.0, 20.0, 0.5, lambda x: f"{x:.1f} s")
        add_scale("历史字幕数量", "history_count", 0, 5, 1, lambda x: f"{int(round(x))} 句")

        for label, key in (
            ("当前日语颜色", "current_color"),
            ("中文翻译颜色", "translation_color"),
            ("历史字幕颜色", "previous_color"),
            ("背景颜色", "background_color"),
        ):
            self.tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            btn = self.tk.Button(frame, text="选择颜色", width=12, bg=getattr(self, key),
                                 command=lambda k=key, t=label: self._choose_color(k, t))
            btn.grid(row=row, column=1, sticky="w", pady=4)
            setattr(self, f"_color_btn_{key}", btn)
            row += 1

        checks = self.tk.Frame(frame)
        checks.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        self.tk.Checkbutton(checks, text="窗口始终置顶", variable=v["topmost"],
                            command=lambda: self._settings_changed("topmost")).pack(side="left")
        self.tk.Checkbutton(checks, text="显示历史字幕", variable=v["show_previous"],
                            command=lambda: self._settings_changed("show_previous")).pack(side="left", padx=(12, 0))
        self.tk.Checkbutton(checks, text="当前字幕粗体", variable=v["current_bold"],
                            command=lambda: self._settings_changed("current_bold")).pack(side="left", padx=(12, 0))
        row += 1

        note = self.tk.Label(frame, text="字幕外观即时预览；模型和识别参数在下次启动生效。设置会自动保存。",
                             fg="#666666", anchor="w")
        note.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1

        buttons = self.tk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.tk.Button(buttons, text="恢复默认", command=self._reset_settings, width=10).pack(side="left", padx=(0, 8))
        self.tk.Button(buttons, text="保存并关闭", command=self._close_settings, width=12).pack(side="left")

        win.update_idletasks()
        x = self.root.winfo_x() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = max(20, self.root.winfo_y() - win.winfo_height() - 12)
        win.geometry(f"+{x}+{y}")
        win.lift()

    # ---- 布局 ----
    def _relayout(self, first=False, anchor_to_screen=False, center_x=None):
        # 默认按真实字体行高自动计算；用户拖动上下边缘后优先保留手动高度。
        history_lines = self.history_count if self.show_previous else 0
        auto_h = (self.f_prev.metrics("linespace") * history_lines + self.f_cur.metrics("linespace") * 2
                  + self.f_trans.metrics("linespace") * 2 + 34)
        h = self.window_height if self.window_height > 0 else auto_h
        if first:
            x = (self.root.winfo_screenwidth() - self.w) // 2
            y = self.sh - h - self.bottom
            self.root.geometry(f"{self.w}x{h}+{x}+{y}")
        elif anchor_to_screen:
            x = self.root.winfo_x()
            y = self.root.winfo_screenheight() - h - self.bottom
            self.root.geometry(f"{self.w}x{h}+{x}+{max(0, y)}")
        else:
            # 保持底边不动
            y_bottom = self.root.winfo_y() + self.root.winfo_height()
            x = self.root.winfo_x()
            if center_x is not None:
                x = int(center_x - self.w / 2)
                x = max(0, min(self.root.winfo_screenwidth() - self.w, x))
            self.root.geometry(f"{self.w}x{h}+{x}+{y_bottom - h}")
        self.h = h

    def _resize_edge_at(self, e):
        margin = 9
        rx = e.x_root - self.root.winfo_rootx()
        ry = e.y_root - self.root.winfo_rooty()
        w = max(1, self.root.winfo_width())
        h = max(1, self.root.winfo_height())
        edge = ""
        if rx <= margin:
            edge += "w"
        elif rx >= w - margin:
            edge += "e"
        if ry <= margin:
            edge += "n"
        elif ry >= h - margin:
            edge += "s"
        return edge

    def _resize_cursor(self, e):
        edge = self._resize_edge_at(e)
        cursor = "sizing" if len(edge) == 2 else (
            "sb_h_double_arrow" if edge in ("w", "e") else
            "sb_v_double_arrow" if edge in ("n", "s") else ""
        )
        try:
            e.widget.configure(cursor=cursor)
        except Exception:
            pass

    def _drag_start(self, e):
        self._resize_edge = self._resize_edge_at(e)
        if self._resize_edge:
            self._resize_origin = (
                e.x_root, e.y_root,
                self.root.winfo_x(), self.root.winfo_y(),
                self.root.winfo_width(), self.root.winfo_height(),
            )
            return
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        edge = getattr(self, "_resize_edge", "")
        if not edge:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
            return

        sx, sy, x0, y0, w0, h0 = self._resize_origin
        dx, dy = e.x_root - sx, e.y_root - sy
        min_w = max(320, int(self.root.winfo_screenwidth() * 0.30))
        min_h = 80
        x, y, w, h = x0, y0, w0, h0

        if "e" in edge:
            w = max(min_w, w0 + dx)
        elif "w" in edge:
            w = max(min_w, w0 - dx)
            x = x0 + (w0 - w)

        if "s" in edge:
            h = max(min_h, h0 + dy)
        elif "n" in edge:
            h = max(min_h, h0 - dy)
            y = y0 + (h0 - h)

        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.w, self.h = w, h
        self.width_frac = max(0.30, min(1.0, w / self.root.winfo_screenwidth()))
        self.window_height = h
        wrap = max(280, w - 40)
        self.l_prev.configure(wraplength=wrap)
        self.l_trans.configure(wraplength=wrap)
        if self._setting_vars:
            self._setting_vars["width_frac"].set(self.width_frac)

    def _drag_end(self, e):
        if getattr(self, "_resize_edge", ""):
            self._resize_edge = ""
            self._schedule_save()

    def _wheel(self, e):
        self.font_size = max(12, min(80, self.font_size + (2 if e.delta > 0 else -2)))
        if self._setting_vars:
            self._setting_vars["font_size"].set(self.font_size)
        self._apply_style()
        self._schedule_save()

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
                    keep = max(1, self.history_count + 1)
                    self.finals = (self.finals + [text])[-keep:]
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
            cur = self.partial_text
            history = self.finals[-self.history_count:] if self.history_count > 0 else []
        else:
            cur = self.finals[-1] if self.finals else ""
            if self.history_count > 0 and len(self.finals) > 1:
                history = self.finals[-(self.history_count + 1):-1]
            else:
                history = []
        translated = self.translation_text
        prev = "\n".join(history)
        self.l_prev.configure(text=prev if self.show_previous else "")
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
            self._save_settings()
            self._close_asr_settings()
            if self.settings_win is not None:
                try:
                    self.settings_win.destroy()
                except Exception:
                    pass
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

    def partial_stable(self, text, stable):
        for s in self.sinks:
            fn = getattr(s, "partial_stable", None)
            if fn:
                fn(text, stable)
            else:
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

    def __init__(self, server_exe, model, mmproj, lang, hotwords, use_context, port=8765, url=None, slots=2,
                 ctx_size=8192, context_history=1, context_chars=80, max_tokens=200, temperature=0.0):
        import subprocess, urllib.request, socket, json
        self.urllib = urllib.request
        self.lang = QWEN_LANG.get(lang, lang) if lang else None
        self.hot = hotwords or ""
        self.use_context = use_context
        self.context_history = max(1, int(context_history))
        self.context_chars = max(40, int(context_chars))
        self.max_tokens = max(64, int(max_tokens))
        self.temperature = max(0.0, min(1.0, float(temperature)))
        self.history = []
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
                # 实时 ASR 每个音频 prompt 都不同；llama-server 默认 8 GiB host prompt cache
                # 会持续填满主机内存而几乎无法复用，因此显式关闭。
                cmd = [server_exe, "-m", model, "--mmproj", mmproj, "-ngl", "99", "--port", str(port),
                       "-c", str(ctx_size), "--no-webui", "-np", str(slots),
                       "--cache-ram", "0", "--no-cache-prompt", "--log-disable"]
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

    def add_context(self, text):
        if text:
            self.history = (self.history + [text])[-self.context_history:]

    def _context_text(self):
        if not self.use_context or not self.history:
            return ""
        return "\n".join(self.history)[-self.context_chars:]

    def _prompt(self):
        ctx = self.hot
        history = self._context_text()
        if history:
            ctx = (ctx + "\n" + history).strip()
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
        history = self._context_text()
        if history:
            ctx = (ctx + "\n" + history).strip()
        msgs = [{"role": "system", "content": ctx},
                {"role": "user", "content": [{"type": "input_audio",
                                              "input_audio": {"data": self._wav_b64(audio), "format": "wav"}}]}]
        # 与 server 侧 --cache-ram 0 保持一致，避免音频请求进入 prompt cache。
        body = json.dumps({"messages": msgs, "max_tokens": self.max_tokens,
                           "temperature": self.temperature, "cache_prompt": False}).encode()
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


def _common_prefix(texts):
    texts = [t for t in texts if t]
    if not texts:
        return ""
    prefix = texts[0]
    for text in texts[1:]:
        n = min(len(prefix), len(text))
        i = 0
        while i < n and prefix[i] == text[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return prefix


class _PartialStabilizer:
    """商业字幕式 provisional/stable 状态：前缀连续多次一致后锁定，尾部继续允许修改。"""
    def __init__(self, confirm_count=3):
        self.confirm_count = max(2, int(confirm_count))
        self.history = collections.deque(maxlen=self.confirm_count)
        self.stable = ""
        self.display = ""
        self.conflicts = 0

    def reset(self):
        self.history.clear()
        self.stable = ""
        self.display = ""
        self.conflicts = 0

    def update(self, raw_text):
        candidate = _clean(raw_text)
        self.history.append(candidate)

        if len(self.history) == self.confirm_count:
            common = _common_prefix(self.history)
            if common.startswith(self.stable) and len(common) > len(self.stable):
                self.stable = common

        # Qwen3-ASR 不是 stateful streaming 模型。单次冲突先抑制，避免字幕抖动；
        # 如果连续 confirm_count 次都冲突，则认为旧 stable 已过时，允许回退并纠正。
        if self.stable and not candidate.startswith(self.stable):
            self.conflicts += 1
            if self.conflicts < self.confirm_count:
                return self.display, self.stable
            self.stable = _common_prefix([self.stable, candidate])
            self.history.clear()
            self.history.append(candidate)
            self.conflicts = 0
        else:
            self.conflicts = 0

        self.display = candidate
        return candidate, self.stable


_PUNC_EOS = "。！？?!"
_HESITATION_ENDINGS = (
    "あの", "あのー", "えー", "ええと", "えっと", "その", "そのー",
    "だから", "それで", "でも", "そして", "というか", "なんか",
)


def _endpoint_silence(base_silence, partial_text, punct_silence=0.40, hesitation_silence=1.10):
    """根据当前 partial 动态调整 speech-final 静音阈值。"""
    text = (partial_text or "").strip()
    if not text:
        return base_silence
    if text[-1] in _PUNC_EOS:
        return min(base_silence, punct_silence)
    stripped = text.rstrip("、，, ")
    if any(stripped.endswith(x) for x in _HESITATION_ENDINGS):
        return max(base_silence, hesitation_silence)
    return base_silence


def run_asr(args, sink, stop, status=None):
    from faster_whisper.vad import get_speech_timestamps, VadOptions, get_vad_model

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
    be = LlamaBackend(args.llama_server, model, mmproj, args.lang, hot, args.context,
                      args.llama_port, args.llama_url, args.slots, args.ctx_size,
                      args.context_history, args.context_chars, args.max_tokens, args.temperature)
    get_vad_model()
    be.warmup()
    print(f"模型就绪 {time.time() - t:.1f}s。开始监听。\n", flush=True)
    if status:
        status("● 监听中")
    dbg = DebugLog(args.debug) if args.debug else None

    # ---- 双路径：Final 永远完整重识别；Partial 只留最新并走 full-context + recoverable stable prefix ----
    cv = threading.Condition()
    final_q = collections.deque()
    partial_slot = [None]
    last_final_seg = [-1]
    active_seg = [0]
    partial_live = {"seg": -1, "text": "", "stable": "", "updated": 0.0}
    partial_live_lock = threading.Lock()
    partial_stabilizer = _PartialStabilizer(getattr(args, "stable_count", 3))
    stabilizer_seg = [-1]

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
            try:
                # Final 始终对完整 utterance 做 second pass，不复用 partial。
                text = be.transcribe(job["audio"])
            except Exception as e:
                print("识别出错:", repr(e), flush=True); continue
            t1 = time.time()
            last_final_seg[0] = job["seg"]
            if text:
                text = _clean(text)
                be.add_context(text)
                sink.final(text, job["t_start"], job["t_end"])
            log_dbg(job, text, t0, t1, False)

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
            if job["seg"] <= last_final_seg[0] or job["seg"] != active_seg[0]:
                continue                          # final/切句后到达的旧 partial 直接作废
            if job["seg"] != stabilizer_seg[0]:
                partial_stabilizer.reset()
                stabilizer_seg[0] = job["seg"]
            display, stable = partial_stabilizer.update(text)
            with partial_live_lock:
                partial_live.update({"seg": job["seg"], "text": display, "stable": stable, "updated": t1})
            if display:
                fn = getattr(sink, "partial_stable", None)
                if fn:
                    fn(display, stable)
                else:
                    sink.partial(display)
            log_dbg(job, display, t0, t1)

    ths = [threading.Thread(target=worker_final, daemon=True), threading.Thread(target=worker_partial, daemon=True)]
    for th in ths:
        th.start()

    def submit(kind, audio, seg, t_start, t_end, reason=None):
        job = {"kind": kind, "audio": audio, "seg": seg, "t_start": t_start, "t_end": t_end,
               "t_q": time.time(), "reason": reason}
        with cv:
            if kind == "final":
                final_q.append(job)
            else:
                partial_slot[0] = job
            cv.notify_all()

    vad_opts = VadOptions(threshold=args.vad_threshold,
                          min_speech_duration_ms=args.vad_min_speech_ms,
                          min_silence_duration_ms=args.vad_min_silence_ms,
                          speech_pad_ms=args.vad_speech_pad_ms)
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
            with partial_live_lock:
                live_text = partial_live["text"] if partial_live["seg"] == seg_id else ""

            # speech-final 与 stable 分离：标点可更快结束，犹豫/连接词则延长等待。
            endpoint_silence = _endpoint_silence(
                args.silence, live_text,
                getattr(args, "punct_silence", 0.40),
                getattr(args, "hesitation_silence", 1.10),
            )
            hard_silence = max(endpoint_silence * 2.5, args.silence * 2.5)

            reason = None
            if (sil >= endpoint_silence and seg_len >= args.min_seg) or sil >= hard_silence:
                reason = "silence"
            elif seg_len >= args.max_seg:
                reason = "maxlen"
            if reason:
                submit("final", buf.copy(), seg_id, seg_start, last_speech, reason)
                seg_id += 1
                active_seg[0] = seg_id
                with partial_live_lock:
                    partial_live.update({"seg": seg_id, "text": "", "stable": "", "updated": now})
                buf = np.zeros(0, dtype=np.float32)
                seg_start = last_speech = None
            elif not args.no_partial and now - last_partial >= args.partial_every and seg_len >= 1.0:
                last_partial = now
                # 非 stateful ASR 使用完整当前 utterance。RTX 3090 的实测性能足以支撑
                # 0.6s cadence，同时避免 rolling window 无时间戳对齐造成重复/错拼。
                submit("partial", buf.copy(), seg_id, seg_start, last_speech)
    finally:
        stop.set()
        try:
            stream.stop_stream(); stream.close()
        except Exception:
            pass
        pa.terminate()
        if len(buf) > SR * 0.5:
            text = be.transcribe(buf)
            if text:
                sink.final(_clean(text), seg_start or time.time(), time.time())
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
    ap.add_argument("--context-history", type=int, default=1, help="作为上下文保留的最终字幕句数")
    ap.add_argument("--context-chars", type=int, default=80, help="上下文最多保留多少字符")
    ap.add_argument("--ctx-size", type=int, default=8192, help="llama-server 总 context size")
    ap.add_argument("--max-tokens", type=int, default=200, help="单次 ASR 最大输出 token 数")
    ap.add_argument("--temperature", type=float, default=0.0, help="ASR 解码温度")
    ap.add_argument("--partial-every", type=float, default=0.4, help="说话中多少秒刷新一次临时结果")
    ap.add_argument("--stable-count", type=int, default=3, help="连续多少次 partial 一致后锁定 stable prefix")
    ap.add_argument("--punct-silence", type=float, default=0.40, help="partial 已有句末标点时的快速句尾静音秒数")
    ap.add_argument("--hesitation-silence", type=float, default=1.10, help="partial 以犹豫/连接词结尾时的延长静音秒数")
    ap.add_argument("--silence", type=float, default=0.5, help="静音多少秒算一句结束")
    ap.add_argument("--vad-win", type=float, default=0.2, help="VAD 判定窗口秒数（越小句尾反应越快）")
    ap.add_argument("--vad-threshold", type=float, default=0.45, help="Silero VAD 语音阈值")
    ap.add_argument("--vad-min-speech-ms", type=int, default=100, help="VAD 最短语音毫秒")
    ap.add_argument("--vad-min-silence-ms", type=int, default=200, help="VAD 最短静音毫秒")
    ap.add_argument("--vad-speech-pad-ms", type=int, default=100, help="VAD 语音前后 padding 毫秒")
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
    def cli_explicit(option):
        return any(a == option or a.startswith(option + "=") for a in sys.argv[1:])

    setting_args = (
        ("context_history", "--context-history"),
        ("context_chars", "--context-chars"),
        ("ctx_size", "--ctx-size"),
        ("slots", "--slots"),
        ("max_tokens", "--max-tokens"),
        ("temperature", "--temperature"),
        ("partial_every", "--partial-every"),
        ("stable_count", "--stable-count"),
        ("punct_silence", "--punct-silence"),
        ("hesitation_silence", "--hesitation-silence"),
        ("silence", "--silence"),
        ("vad_win", "--vad-win"),
        ("vad_threshold", "--vad-threshold"),
        ("vad_min_speech_ms", "--vad-min-speech-ms"),
        ("vad_min_silence_ms", "--vad-min-silence-ms"),
        ("vad_speech_pad_ms", "--vad-speech-pad-ms"),
        ("min_seg", "--min-seg"),
        ("max_seg", "--max-seg"),
        ("max_gain", "--max-gain"),
    )
    for attr, option in setting_args:
        if not cli_explicit(option):
            source_attr = "llama_ctx" if attr == "ctx_size" else attr
            setattr(args, attr, getattr(overlay, source_attr))
    if not cli_explicit("--no-context"):
        args.context = overlay.context_enabled
    if args.model is None:
        here = os.path.dirname(os.path.abspath(__file__))
        model_names = {
            "Q8_0": "Qwen3-ASR-1.7B-Q8_0.gguf",
            "BF16": "Qwen3-ASR-1.7B-bf16.gguf",
        }
        selected_name = model_names.get(overlay.asr_model, model_names["Q8_0"])
        selected_path = os.path.join(here, "models", selected_name)
        if os.path.isfile(selected_path):
            args.model = selected_path
        else:
            fallback = os.path.join(here, "models", model_names["Q8_0"])
            if overlay.asr_model != "Q8_0" and os.path.isfile(fallback):
                print(f"所选模型 {selected_name} 不存在，自动回退到 Q8_0。", flush=True)
                overlay.status("⚠ BF16 不存在，已回退 Q8_0")
                args.model = fallback
            else:
                args.model = selected_path
    print(f"ASR 主模型: {os.path.basename(args.model)}", flush=True)
    print(
        f"识别参数: ctx={args.ctx_size} slots={args.slots} context={args.context_history}句/{args.context_chars}字 "
        f"VAD={args.vad_threshold:.2f} partial={args.partial_every:.2f}s "
        f"stable={args.stable_count} silence={args.punct_silence:.2f}/{args.silence:.2f}/{args.hesitation_silence:.2f}s "
        f"seg={args.min_seg:.1f}-{args.max_seg:.1f}s",
        flush=True,
    )
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
