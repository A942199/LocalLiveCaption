[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python is missing: $python. Run: py -3.11 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# Activate this project's venv in the current launcher process only.
# Unlike calling the old project's python.exe, both subprocesses and
# PATH-based executables now inherit the correct virtual environment.
$env:VIRTUAL_ENV = $venv
$env:PATH = (Join-Path $venv 'Scripts') + [IO.Path]::PathSeparator + $env:PATH
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$config = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Local runtime configuration missing: $config. Copy .env.example to .env and set your existing model/llama paths."
}

# Read only a fixed set of keys; .env is data, not executable PowerShell.
$allowed = @('MODEL_PATH', 'MMPROJ_PATH', 'LLAMA_SERVER_PATH', 'ASR_LANGUAGE')
$options = @{}
foreach ($line in [IO.File]::ReadAllLines($config)) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
    if ($trimmed -notmatch '^([A-Z_][A-Z0-9_]*)=(.*)$') {
        throw "Malformed .env entry. Expected KEY=value: $trimmed"
    }
    $key = $Matches[1]
    $value = $Matches[2].Trim()
    if ($key -notin $allowed) { throw "Unsupported .env key: $key" }
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $options[$key] = $value
}

foreach ($key in @('MODEL_PATH','MMPROJ_PATH','LLAMA_SERVER_PATH')) {
    if (-not $options[$key]) { throw "Set $key in .env" }
    $path = $options[$key]
    if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $PSScriptRoot $path }
    $path = [IO.Path]::GetFullPath($path)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $key : $path" }
    $options[$key] = $path
}

$lang = $options['ASR_LANGUAGE']
if (-not $lang) { $lang = 'ja' }
if ($lang -notmatch '^(ja|auto)$') { throw 'ASR_LANGUAGE must be ja or auto' }

# Expose named runtime variables for child processes and diagnostics.
$env:LLC_MODEL_PATH = $options['MODEL_PATH']
$env:LLC_MMPROJ_PATH = $options['MMPROJ_PATH']
$env:LLC_LLAMA_SERVER_PATH = $options['LLAMA_SERVER_PATH']
$env:LLC_ASR_LANGUAGE = $lang

if ($Check) {
    Write-Host "VIRTUAL_ENV=$env:VIRTUAL_ENV"
    Write-Host "Python=$python"
    Write-Host "Model=$env:LLC_MODEL_PATH"
    Write-Host "MMProj=$env:LLC_MMPROJ_PATH"
    Write-Host "LlamaServer=$env:LLC_LLAMA_SERVER_PATH"
    & $python -c "import sys, numpy, pyaudiowpatch, soxr, faster_whisper, huggingface_hub; print('Python:', sys.version.split()[0]); print('Executable:', sys.executable); print('Environment ready')"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -B 'live-caption-ja.pyw' --help | Select-Object -First 4
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    exit 0
}

if ($Test) {
    & $python -B -m unittest discover -s tests -p 'test_*.py' -q
    exit $LASTEXITCODE
}

& $python -B 'live-caption-ja.pyw' --model $options['MODEL_PATH'] --mmproj $options['MMPROJ_PATH'] --llama-server $options['LLAMA_SERVER_PATH'] --lang $lang
exit $LASTEXITCODE
