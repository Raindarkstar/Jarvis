$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $PyLauncher) {
    $PythonExecutable = $PyLauncher.Source
    $PythonArguments = @("-3.12")
} else {
    $PythonExecutable = (Get-Command python -ErrorAction Stop).Source
    $PythonArguments = @()
}

& $PythonExecutable @PythonArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 or newer is required. Install it from https://www.python.org/downloads/windows/"
}

Write-Host "Creating virtual environment in $VenvPath"
if (-not (Test-Path $VenvPython)) {
    & $PythonExecutable @PythonArguments -m venv $VenvPath
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install --editable $ProjectRoot

$EnvExample = Join-Path $ProjectRoot ".env.example"
$EnvFile = Join-Path $ProjectRoot ".env"
if ((Test-Path $EnvExample) -and (-not (Test-Path $EnvFile))) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "Created .env from .env.example; add DASHSCOPE_API_KEY before launching."
}

Write-Host ""
Write-Host "Jarvis Windows voice assistant installed."
Write-Host "  Wake:   python .\jarvis.py"
Write-Host "  Voice:  .\.venv\Scripts\jarvis.exe voice"
Write-Host "  UI:     .\.venv\Scripts\jarvis.exe desktop"
Write-Host "  Check:  .\.venv\Scripts\jarvis.exe doctor"
