@echo off
title TensorCash — import key
cd /d "%~dp0"
set DATADIR=%~dp0tensorcash-data

:: Start wallet if not running
set /p KEY="Enter private key (won't show): " <nul
set KEY=
for /f "delims=" %%a in ('xcopy /w "%~f0" "%~f0" 2^>nul') do if not defined KEY set "KEY=%%a"

powershell -NoProfile -Command "& {
  $k=%KEY%;
  $c=Get-Content '%DATADIR%\.cookie' -Raw;
  $b=@{jsonrpc='1.0';id='1';method='importprivkey';params=@($k)} | ConvertTo-Json -Compress;
  try { Invoke-RestMethod http://127.0.0.1:39240 -Post -Body $b -ContentType 'text/plain' -Headers @{Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($c.Trim()))} -TimeoutSec 300 }
  catch { 'Error: '+$_ }
}"
pause
