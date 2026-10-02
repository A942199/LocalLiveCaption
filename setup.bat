@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] Python venv ...
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
echo [2/3] Model (Qwen3-ASR-1.7B GGUF, ~2.8GB) ...
.venv\Scripts\python.exe -m pip install -q huggingface_hub
.venv\Scripts\python.exe -c "from huggingface_hub import hf_hub_download as d; [d('ggml-org/Qwen3-ASR-1.7B-GGUF', f, local_dir='models') for f in ('Qwen3-ASR-1.7B-Q8_0.gguf','mmproj-Qwen3-ASR-1.7B-bf16.gguf')]"
echo [3/3] llama.cpp: put a recent Windows CUDA build (b10900+ / after 2026-04-12) into the llama\ folder next to this script.
echo       https://github.com/ggml-org/llama.cpp/releases  ^(llama-bXXXX-bin-win-cuda-13.x-x64.zip + cudart-llama-bin-win-cuda-13.x-x64.zip^)
if not exist llama\llama-server.exe echo       llama\llama-server.exe NOT found yet.
if not exist hotwords.txt copy hotwords.example.txt hotwords.txt >nul
echo Done. Run start-zh.bat (Chinese) or start.bat (auto language).
pause