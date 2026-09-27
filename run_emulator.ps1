# Запуск эмулятора участка Люблино одной командой (Windows).
# Использование: ./run_emulator.ps1 [-Port 8100] [-Window] [другие параметры python -m emulator]
param(
    [int]$Port = 8100,
    [switch]$Window,
    [Parameter(ValueFromRemainingArguments = $true)] $Rest
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "==> Создаю виртуальное окружение .venv"
    python -m venv .venv
}
& $py -c "import panda3d, cv2, fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Ставлю зависимости эмулятора"
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -r emulator\requirements.txt
}

$argsList = @("-m", "emulator", "--port", $Port)
if ($Window) { $argsList += "--window" }
if ($Rest) { $argsList += $Rest }
$env:PYTHONIOENCODING = "utf-8"
& $py @argsList
