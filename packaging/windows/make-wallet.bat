@echo off
title TensorCash — make wallet
cd /d "%~dp0"
set DATADIR=%~dp0tensorcash-data

:: start fresh
del "%DATADIR%\tensor\.lock" "%DATADIR%\tensor\bitcoind.pid" 2>nul
start /B "" "%~dp0tensorcash-qt.exe" -datadir="%DATADIR%"

echo Waiting for wallet... 
:wait
timeout /t 3 /nobreak >nul
curl -s -u tc:tc http://127.0.0.1:39240 >nul 2>&1
if errorlevel 1 goto wait

echo Importing key...
curl -s -u tc:tc -X POST http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"importprivkey\",\"params\":[\"Y9V6meV9oknSVpk5gZm4eqRuWvmtLcn2N8xwmec3cffyAAmhDpa4\"]}"

echo.
echo Checking balance...
curl -s -u tc:tc -X POST http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"getbalance\"}"
echo.
pause