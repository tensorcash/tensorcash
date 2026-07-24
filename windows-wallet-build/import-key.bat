@echo off
title TensorCash — import private key
cd /d "%~dp0"
set DATADIR=%~dp0tensorcash-data

:: Start wallet if not running
if not exist "%DATADIR%" mkdir "%DATADIR%"
if not exist "%DATADIR%\.cookie" (
  echo Starting wallet...
  start "" "%~dp0tensorcash-qt.exe" -datadir="%DATADIR%"
  :wait
  timeout /t 2 /nobreak >nul
  if not exist "%DATADIR%\.cookie" goto wait
)

:: Prompt for key, call RPC
for /f "usebackq delims=" %%a in ("%DATADIR%\.cookie") do set COOKIE=%%a
set /p KEY=Paste WIF private key: 
echo.
powershell -NoLogo -Command ^
  $c = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('%COOKIE%')); ^
  $b = (@{jsonrpc='1.0';id='1';method='importprivkey';params=@('%KEY%')} | ConvertTo-Json -Compress); ^
  try { ^
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:39240' -Method Post -Body $b -ContentType 'text/plain' -Headers @{Authorization=('Basic ' + $c)} -TimeoutSec 300; ^
    if ($r.error) { Write-Host ('RPC error: ' + $r.error.message) -Foreground Red } ^
    else { Write-Host 'OK — key imported!' -Foreground Green } ^
  } catch { Write-Host ('Error: ' + $_.Exception.Message) -Foreground Red }
echo.
pause
