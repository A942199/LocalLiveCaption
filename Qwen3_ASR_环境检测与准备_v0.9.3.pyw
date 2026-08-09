# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "NemoSubtitle"
TEN_VAD_DOWNLOAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.onnx"


def default_user_data_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
        return os.path.join(base, APP_NAME)
    base = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, APP_NAME)


DATA_DIR = default_user_data_dir()
MODELS_DIR = os.path.join(DATA_DIR, "models")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(DATA_DIR, "qwen-runtime.json")
BOOTSTRAP_LOG = os.path.join(LOGS_DIR, "environment-preflight.log")
os.makedirs(MODELS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


class EnvironmentPreflight:
    DESKTOP_CORE = (
        ("numpy", "numpy>=1.26"),
        ("scipy", "scipy>=1.11"),
        ("sherpa_onnx", "sherpa-onnx"),
    )
    TRANSLATION_DEPS = (
        ("ctranslate2", "ctranslate2>=4.5,<5"),
        ("huggingface_hub", "huggingface_hub"),
        ("transformers", "transformers"),
        ("sentencepiece", "sentencepiece>=0.2"),
    )

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root: Optional[tk.Tk] = None
        self.status_var: Optional[tk.StringVar] = None
        self.detail_widget: Optional[tk.Text] = None
        self.wsl_distro = str(args.wsl_distro or "").strip()
        self.wsl_python = str(args.wsl_python or "").strip()
        self._cancelled = False

    @staticmethod
    def _decode_output(raw: bytes) -> str:
        if not raw:
            return ""
        if raw.count(b"\x00") > max(2, len(raw) // 8):
            for enc in ("utf-16-le", "utf-16"):
                try:
                    return raw.decode(enc).replace("\x00", "").strip()
                except UnicodeDecodeError:
                    pass
        for enc in ("utf-8", "utf-8-sig", "cp932", "mbcs"):
            try:
                return raw.decode(enc).replace("\x00", "").strip()
            except (UnicodeDecodeError, LookupError):
                pass
        return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()

    @staticmethod
    def _hidden_kwargs() -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        return kwargs

    def _open_ui(self) -> None:
        root = tk.Tk()
        root.title("Qwen3-ASR 前置环境检测与准备")
        root.geometry("720x400")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.status_var = tk.StringVar(value="正在检测运行环境...")
        tk.Label(root, textvariable=self.status_var, anchor="w", font=("Microsoft YaHei UI", 11, "bold")).pack(
            fill="x", padx=16, pady=(16, 8)
        )
        progress = ttk.Progressbar(root, mode="indeterminate")
        progress.pack(fill="x", padx=16, pady=(0, 10))
        progress.start(12)
        detail = tk.Text(root, height=16, wrap="word", state="disabled")
        detail.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.root = root
        self.detail_widget = detail
        root.update_idletasks()
        root.update()

    def _cancel(self) -> None:
        if messagebox.askyesno("取消", "确定取消环境检测/准备吗？", parent=self.root):
            self._cancelled = True
            if self.root is not None:
                self.root.destroy()
                self.root = None

    def _pump(self) -> None:
        if self.root is None:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self._cancelled = True
            self.root = None

    def _status(self, text: str) -> None:
        if self.status_var is not None:
            self.status_var.set(text)
        if self.detail_widget is not None:
            try:
                self.detail_widget.config(state="normal")
                self.detail_widget.insert("end", text.rstrip() + "\n")
                self.detail_widget.see("end")
                self.detail_widget.config(state="disabled")
            except tk.TclError:
                pass
        self._pump()

    def _wsl_prefix(self) -> List[str]:
        command = ["wsl.exe"]
        if self.wsl_distro:
            command += ["--distribution", self.wsl_distro]
        return command

    def _run_capture(self, command: List[str], *, timeout: float = 30.0) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                **self._hidden_kwargs(),
            )
            return int(completed.returncode), self._decode_output(completed.stdout or b"")
        except subprocess.TimeoutExpired as exc:
            return 124, self._decode_output(exc.stdout or b"") + "\n命令执行超时"
        except Exception as exc:
            return 127, str(exc)

    def _run_wsl_shell(self, script: str, *, timeout: float = 30.0) -> tuple[int, str]:
        return self._run_capture(self._wsl_prefix() + ["--exec", "sh", "-lc", script], timeout=timeout)

    def _run_logged(self, command: List[str], status: str, *, timeout: float) -> tuple[bool, str]:
        self._status(status)
        started = time.monotonic()
        with open(BOOTSTRAP_LOG, "a", encoding="utf-8", errors="replace") as log:
            log.write("\n$ " + subprocess.list2cmdline(command) + "\n")
            log.flush()
            try:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **self._hidden_kwargs())
            except Exception as exc:
                return False, str(exc)
            while process.poll() is None:
                if self._cancelled:
                    process.terminate()
                    return False, "用户取消"
                if time.monotonic() - started > timeout:
                    process.terminate()
                    return False, "命令执行超时"
                self._pump()
                time.sleep(0.12)
        return process.returncode == 0, self._tail(BOOTSTRAP_LOG, 35)

    @staticmethod
    def _tail(path: str, lines: int) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=lines)).strip()
        except OSError:
            return ""

    def _desktop_python(self) -> str:
        exe = os.path.abspath(sys.executable)
        if exe.lower().endswith("pythonw.exe"):
            py = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(py):
                return py
        return exe

    def _missing_desktop(self) -> List[tuple[str, str]]:
        required = list(self.DESKTOP_CORE)
        if self.args.capture_source in ("system", "both"):
            required.append(("pyaudiowpatch", "PyAudioWPatch"))
        elif importlib.util.find_spec("pyaudiowpatch") is None and importlib.util.find_spec("pyaudio") is None:
            required.append(("pyaudiowpatch", "PyAudioWPatch"))
        if self.args.translation_backend == "nllw":
            required.extend(self.TRANSLATION_DEPS)
        return [(m, p) for m, p in required if importlib.util.find_spec(m) is None]

    def _repair_yes(self, title: str, text: str) -> bool:
        if self.args.auto_repair:
            return True
        if self.args.no_auto_repair:
            return False
        return bool(messagebox.askyesno(title, text + "\n\n选择“是”自动修复。", parent=self.root))

    def _install_desktop(self, missing: List[tuple[str, str]]) -> bool:
        packages = []
        for _m, package in missing:
            if package not in packages:
                packages.append(package)
        command = [self._desktop_python(), "-m", "pip", "install", "-U"] + packages
        ok, detail = self._run_logged(command, "正在安装 Windows 端缺失依赖...", timeout=1800)
        if not ok:
            messagebox.showerror("Windows 依赖安装失败", detail[-4000:] + f"\n\n日志：{BOOTSTRAP_LOG}", parent=self.root)
        return ok

    def _ensure_ten_vad(self) -> str:
        target = os.path.abspath(os.path.expanduser(self.args.vad_model_path))
        if os.path.isfile(target) and os.path.getsize(target) > 100_000:
            self._status(f"TEN-VAD：{target}")
            return target
        if not self._repair_yes("缺少 TEN-VAD", f"未找到 ten-vad.onnx。\n\n是否下载到：\n{target}"):
            return ""
        self._status("正在下载 TEN-VAD...")
        tmp = target + ".download"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            req = urllib.request.Request(TEN_VAD_DOWNLOAD_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as response, open(tmp, "wb") as out:
                while True:
                    if self._cancelled:
                        raise RuntimeError("用户取消")
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    out.write(block)
                    self._pump()
            if os.path.getsize(tmp) <= 100_000:
                raise RuntimeError("TEN-VAD 下载文件尺寸异常")
            os.replace(tmp, target)
            self._status(f"TEN-VAD：准备完成 {target}")
            return target
        except Exception as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            messagebox.showerror("TEN-VAD 下载失败", str(exc), parent=self.root)
            return ""

    def _resolve_wsl(self) -> bool:
        if shutil.which("wsl.exe") is None:
            messagebox.showerror("WSL 未安装", "未检测到 wsl.exe。", parent=self.root)
            return False
        if self.wsl_distro:
            code, out = self._run_capture([
                "wsl.exe", "--distribution", self.wsl_distro, "--exec", "sh", "-lc",
                "printf '%s' \"$WSL_DISTRO_NAME\"",
            ])
            if code == 0:
                if out.strip():
                    self.wsl_distro = out.strip().splitlines()[-1]
                self._status(f"WSL：{self.wsl_distro} 可用")
                return True
            messagebox.showerror("WSL 不可用", out, parent=self.root)
            return False
        code, out = self._run_capture(["wsl.exe", "--exec", "sh", "-lc", "printf '%s' \"$WSL_DISTRO_NAME\""])
        if code == 0:
            self.wsl_distro = out.strip().splitlines()[-1] if out.strip() else ""
            self._status(f"WSL：{self.wsl_distro or '默认发行版'} 可用")
            return True
        list_code, listed = self._run_capture(["wsl.exe", "--list", "--quiet"])
        distros = [x.strip().lstrip("* ").strip() for x in listed.splitlines() if x.strip()]
        if list_code == 0 and distros:
            self.wsl_distro = distros[0]
            code, _ = self._run_capture(["wsl.exe", "--distribution", self.wsl_distro, "--exec", "true"])
            if code == 0:
                self._status(f"WSL：自动选择 {self.wsl_distro}")
                return True
        messagebox.showerror("WSL 不可用", out or listed, parent=self.root)
        return False

    def _probe_gpu(self) -> bool:
        script = r'''
set +e
SMI="$(command -v nvidia-smi 2>/dev/null || true)"
if [ -z "$SMI" ]; then
  for candidate in /usr/lib/wsl/lib/nvidia-smi /usr/bin/nvidia-smi /usr/local/bin/nvidia-smi; do
    if [ -x "$candidate" ]; then SMI="$candidate"; break; fi
  done
fi
if [ -n "$SMI" ]; then
  OUT="$($SMI --query-gpu=name,memory.total --format=csv,noheader 2>&1)"
  if [ $? -eq 0 ] && [ -n "$OUT" ]; then
    printf '__SMI__=%s\n' "$SMI"
    printf '%s\n' "$OUT"
    exit 0
  fi
  OUT="$($SMI -L 2>&1)"
  if [ $? -eq 0 ] && [ -n "$OUT" ]; then
    printf '__SMI__=%s\n' "$SMI"
    printf '%s\n' "$OUT"
    exit 0
  fi
fi
DXG=0; CUDA_LIB=0
[ -e /dev/dxg ] && DXG=1
if [ -e /usr/lib/wsl/lib/libcuda.so.1 ] || [ -e /usr/lib/wsl/lib/libcuda.so ]; then CUDA_LIB=1; fi
printf '__DXG__=%s\n__LIBCUDA__=%s\n' "$DXG" "$CUDA_LIB"
[ "$DXG" = 1 ] && [ "$CUDA_LIB" = 1 ] && exit 0
exit 22
'''
        code, out = self._run_wsl_shell(script, timeout=30)
        if code == 0:
            visible = [x for x in out.splitlines() if x.strip() and not x.startswith("__")]
            self._status("WSL GPU：" + (visible[0] if visible else "CUDA bridge 可用"))
            return True
        messagebox.showerror(
            "WSL CUDA/GPU 不可用",
            "已检查 PATH、/usr/lib/wsl/lib/nvidia-smi、/dev/dxg 与 libcuda。\n\n" + out,
            parent=self.root,
        )
        return False

    def _probe_wsl_python(self) -> str:
        if self.wsl_python:
            candidates = [shlex.quote(self.wsl_python)]
        else:
            candidates = [
                '"$HOME/.cache/qwen3-subtitle/venv/bin/python"',
                '"$HOME/.venv/bin/python"',
                '"$HOME"/.local/share/uv/python/cpython-3.12*/bin/python3',
                '"$HOME"/.local/share/uv/python/cpython-3.11*/bin/python3',
                '"$(command -v python3 2>/dev/null || true)"',
                '"$(command -v python 2>/dev/null || true)"',
            ]
        script = "\n".join([
            "set +e",
            "for py in " + " ".join(candidates) + "; do",
            '  [ -n "$py" ] || continue',
            '  [ -x "$py" ] || continue',
            '  if "$py" -c \'import qwen_asr, vllm, numpy, torch; assert torch.cuda.is_available()\' >/dev/null 2>&1; then',
            '    "$py" -c \'import sys, torch; print("__PY__="+sys.executable); print("__TORCH__="+torch.__version__); print("__GPU__="+torch.cuda.get_device_name(0))\'',
            "    exit 0",
            "  fi",
            "done",
            "exit 9",
        ])
        code, out = self._run_wsl_shell(script, timeout=90)
        if code == 0 and out.strip():
            lines = [x.strip() for x in out.splitlines() if x.strip()]
            py_line = next((x for x in lines if x.startswith("__PY__=")), "")
            torch_line = next((x for x in lines if x.startswith("__TORCH__=")), "")
            gpu_line = next((x for x in lines if x.startswith("__GPU__=")), "")
            if py_line:
                gpu_name = gpu_line.split("=", 1)[1] if gpu_line else "available"
                torch_version = torch_line.split("=", 1)[1] if torch_line else "unknown"
                self._status(f"WSL PyTorch CUDA：{gpu_name} / torch {torch_version}")
                return py_line.split("=", 1)[1].strip()
        return ""

    def _install_wsl_runtime(self) -> bool:
        script = r'''set -eu
ENV_DIR="$HOME/.cache/qwen3-subtitle/venv"
mkdir -p "$HOME/.cache/qwen3-subtitle"
UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then UV_BIN="$HOME/.local/bin/uv"; fi
if [ -n "$UV_BIN" ]; then
  if [ ! -x "$ENV_DIR/bin/python" ]; then "$UV_BIN" venv --python 3.12 "$ENV_DIR"; fi
  "$UV_BIN" pip install --python "$ENV_DIR/bin/python" -U 'qwen-asr[vllm]'
else
  BASE="$(command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || true)"
  [ -n "$BASE" ] || { echo 'Python 3 not found in WSL' >&2; exit 20; }
  if [ ! -x "$ENV_DIR/bin/python" ]; then "$BASE" -m venv "$ENV_DIR"; fi
  "$ENV_DIR/bin/python" -m pip install -U pip setuptools wheel
  "$ENV_DIR/bin/python" -m pip install -U 'qwen-asr[vllm]'
fi
"$ENV_DIR/bin/python" -c 'import qwen_asr, vllm, numpy, torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
'''
        command = self._wsl_prefix() + ["--exec", "sh", "-lc", script]
        ok, detail = self._run_logged(command, "正在准备 WSL qwen-asr/vLLM 环境...", timeout=3600)
        if not ok:
            messagebox.showerror("WSL ASR 环境安装失败", detail[-4000:] + f"\n\n日志：{BOOTSTRAP_LOG}", parent=self.root)
        return ok

    def _save_config(self, vad_path: str) -> None:
        payload = {
            "schema": 1,
            "prepared_at": time.time(),
            "prepared_by": "Qwen3_ASR_环境检测与准备_v0.9.3",
            "desktop_python": self._desktop_python(),
            "wsl_distro": self.wsl_distro,
            "wsl_python": self.wsl_python,
            "vad_model_path": vad_path,
            "qwen_wsl_hf_home": str(self.args.qwen_wsl_hf_home or "").strip(),
            "capture_source": self.args.capture_source,
            "translation_backend": self.args.translation_backend,
        }
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CONFIG_PATH)

    def run(self) -> bool:
        self._open_ui()
        try:
            self._status(f"Windows Python：{sys.version.split()[0]} ({self._desktop_python()})")
            if sys.version_info < (3, 10):
                messagebox.showerror("Python 版本过低", "需要 Python 3.10 或更高。", parent=self.root)
                return False
            missing = self._missing_desktop()
            if missing:
                detail = "\n".join(f"- {m} ← {p}" for m, p in missing)
                self._status("Windows 缺失依赖：\n" + detail)
                if not self._repair_yes("Windows 依赖不完整", "检测到缺失依赖：\n\n" + detail):
                    return False
                if not self._install_desktop(missing):
                    return False
                importlib.invalidate_caches()
                still = self._missing_desktop()
                if still:
                    messagebox.showerror("依赖仍不完整", ", ".join(x[0] for x in still), parent=self.root)
                    return False
            self._status("Windows 桌面依赖：完整")
            vad_path = self._ensure_ten_vad()
            if not vad_path:
                return False
            if not self._resolve_wsl():
                return False
            if not self._probe_gpu():
                return False
            self.wsl_python = self._probe_wsl_python()
            if not self.wsl_python:
                if not self._repair_yes(
                    "WSL ASR 环境不完整",
                    "没有找到同时具备 qwen_asr + vLLM + PyTorch CUDA 的 WSL Python。\n\n是否创建 ~/.cache/qwen3-subtitle/venv？",
                ):
                    return False
                if not self._install_wsl_runtime():
                    return False
                self.wsl_python = self._probe_wsl_python()
            if not self.wsl_python:
                messagebox.showerror("WSL Python 校验失败", "安装后仍无法验证 qwen_asr/vLLM/CUDA。", parent=self.root)
                return False
            self._status(f"WSL Python：{self.wsl_python}")
            self._save_config(vad_path)
            self._status(f"环境准备完成，配置已保存：{CONFIG_PATH}")
            messagebox.showinfo(
                "前置环境检测完成",
                "运行环境已经准备完成。\n\n以后直接运行主程序即可，不会再做完整环境检测。\n\n"
                f"运行配置：{CONFIG_PATH}\n日志：{BOOTSTRAP_LOG}",
                parent=self.root,
            )
            return True
        finally:
            if self.root is not None:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass
                self.root = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Qwen3-ASR 前置环境检测与准备")
    p.add_argument("--capture-source", choices=["system", "mic", "both"], default="system")
    p.add_argument("--translation-backend", choices=["nllw", "off"], default="off")
    p.add_argument("--vad-model-path", default=os.path.join(MODELS_DIR, "ten-vad.onnx"))
    p.add_argument("--wsl-distro", default="")
    p.add_argument("--wsl-python", default="")
    p.add_argument("--qwen-wsl-hf-home", default="")
    p.add_argument("--auto-repair", action="store_true")
    p.add_argument("--no-auto-repair", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        try:
            import multiprocessing
            multiprocessing.freeze_support()
        except Exception:
            pass
    args = parse_args()
    ok = EnvironmentPreflight(args).run()
    raise SystemExit(0 if ok else 3)
