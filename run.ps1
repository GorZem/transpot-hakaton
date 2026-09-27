# Запуск центра и админки одной командой (Windows PowerShell).
# Использование: .\run.ps1 [-Port 8000] [-Cpu]
param([int]$Port = 8000, [switch]$Cpu)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "==> Создаю виртуальное окружение .venv"
    python -m venv .venv
}
$py = ".venv\Scripts\python.exe"
& $py -c "import torch, ultralytics, fastapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Ставлю зависимости (один раз)"
    & $py -m pip install -q --upgrade pip
    $index = if ($Cpu) { "https://download.pytorch.org/whl/cpu" } else { "https://download.pytorch.org/whl/cu128" }
    & $py -m pip install -q torch torchvision --index-url $index
    & $py -m pip install -q -r requirements.txt
}
if (-not (Test-Path "admin\dist\index.html")) {
    Write-Host "==> Собираю админку"
    Push-Location admin
    npm ci
    npm run build
    Pop-Location
}
Write-Host "==> Центр: http://127.0.0.1:$Port  (остановить: Ctrl+C)"
Start-Process "http://127.0.0.1:$Port"
& $py -m center --port $Port
