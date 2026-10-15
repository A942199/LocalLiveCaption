@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo LocalLiveCaption - fresh Windows setup
echo ============================================================

echo [0/4] Checking Python 3.11+ ...
if exist ".venv\Scripts\python.exe" goto :venv_ready

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 (
    python -m venv .venv
    if errorlevel 1 goto :fail
    goto :venv_ready
)

py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    py -3.11 -m venv .venv
    if errorlevel 1 goto :fail
    goto :venv_ready
)

echo Python 3.11+ was not found. Trying to install Python 3.11 with winget ...
where winget >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is missing and winget is unavailable.
    echo Install 64-bit Python 3.11 or newer from python.org, then run setup.bat again.
    goto :fail
)

winget install --id Python.Python.3.11 -e --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :fail

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" -m venv .venv
) else (
    py -3.11 -m venv .venv
)
if errorlevel 1 goto :fail

:venv_ready
echo [1/4] Installing Python dependencies ...
".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo pip was not found in .venv; repairing it with ensurepip ...
    ".venv\Scripts\python.exe" -m ensurepip --upgrade
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto :fail

echo [2/4] Preparing Qwen3-ASR-1.7B GGUF model files (~2.8 GB) ...
if not exist "models\Qwen3-ASR-1.7B-Q8_0.gguf" goto :download_models
if not exist "models\mmproj-Qwen3-ASR-1.7B-bf16.gguf" goto :download_models
echo Existing model files found; keeping them.
goto :models_ready

:download_models
".venv\Scripts\python.exe" -c "from huggingface_hub import hf_hub_download as d; repo='ggml-org/Qwen3-ASR-1.7B-GGUF'; files=('Qwen3-ASR-1.7B-Q8_0.gguf','mmproj-Qwen3-ASR-1.7B-bf16.gguf'); [d(repo, f, local_dir='models') for f in files]"
if errorlevel 1 goto :fail

:models_ready
echo [3/4] Preparing llama.cpp Windows x64 CUDA build ...
if not exist "llama\llama-server.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
      "$headers=@{'User-Agent'='LocalLiveCaption-setup'};" ^
      "$releases=Invoke-RestMethod -Headers $headers -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20';" ^
      "$release=$null; $base=$null;" ^
      "foreach($candidate in $releases) {" ^
      "  if($candidate.draft){continue};" ^
      "  if($candidate.tag_name -notmatch '^b([0-9]+)$'){continue};" ^
      "  if([int]$Matches[1] -lt 10900){continue};" ^
      "  $candidateBase=$candidate.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-(12\.[0-9]+)-x64\.zip$' } | Select-Object -First 1;" ^
      "  if(-not $candidateBase){$candidateBase=$candidate.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-(13\.[0-9]+)-x64\.zip$' } | Select-Object -First 1};" ^
      "  if($candidateBase){$release=$candidate; $base=$candidateBase; break};" ^
      "};" ^
      "if(-not $base){throw 'No compatible Windows x64 CUDA llama.cpp package was found in recent releases.'};" ^
      "$null=$base.name -match 'cuda-((?:12|13)\.[0-9]+)-x64'; $cuda=$Matches[1];" ^
      "$runtimeName='cudart-llama-bin-win-cuda-'+$cuda+'-x64.zip';" ^
      "$runtime=$release.assets | Where-Object { $_.name -eq $runtimeName } | Select-Object -First 1;" ^
      "if(-not $runtime){throw ('Matching CUDA runtime package was not found: '+$runtimeName)};" ^
      "$tmp=Join-Path $env:TEMP ('LocalLiveCaption-'+[guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tmp | Out-Null;" ^
      "try {" ^
      "  $baseZip=Join-Path $tmp 'llama.zip'; $runtimeZip=Join-Path $tmp 'cudart.zip';" ^
      "  Write-Host ('Downloading '+$base.name);" ^
      "  Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $base.browser_download_url -OutFile $baseZip;" ^
      "  Write-Host ('Downloading '+$runtime.name);" ^
      "  Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $runtime.browser_download_url -OutFile $runtimeZip;" ^
      "  New-Item -ItemType Directory -Force -Path 'llama' | Out-Null;" ^
      "  Expand-Archive -LiteralPath $baseZip -DestinationPath 'llama' -Force;" ^
      "  Expand-Archive -LiteralPath $runtimeZip -DestinationPath 'llama' -Force;" ^
      "} finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }"
    if errorlevel 1 goto :fail
) else (
    echo Existing llama\llama-server.exe found; keeping it.
)

if not exist "llama\llama-server.exe" (
    echo ERROR: llama\llama-server.exe is missing after setup.
    goto :fail
)

echo [4/4] Verifying installation ...
if not exist "models\Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo ERROR: ASR model file is missing.
    goto :fail
)
if not exist "models\mmproj-Qwen3-ASR-1.7B-bf16.gguf" (
    echo ERROR: mmproj model file is missing.
    goto :fail
)

".venv\Scripts\python.exe" -c "import numpy, pyaudiowpatch, soxr, faster_whisper, huggingface_hub; print('Python dependencies: OK')"
if errorlevel 1 goto :fail

"llama\llama-server.exe" --version
if errorlevel 1 goto :fail

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo WARNING: nvidia-smi was not found. Install/update the NVIDIA driver before running live captions.
)

echo.
echo ============================================================
echo Setup complete.
echo Double-click live-caption-ja.pyw to start Japanese captions.
echo The first normal launch uses the project's .venv automatically.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ============================================================
echo Setup failed. Review the error above, then run setup.bat again.
echo ============================================================
pause
exit /b 1