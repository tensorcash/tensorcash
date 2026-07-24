@echo off
title TensorCash — wallet file maker
cd /d "%~dp0"

set WIF=Y9V6meV9oknSVpk5gZm4eqRuWvmtLcn2N8xwmec3cffyAAmhDpa4
set DATADIR=%~dp0tensorcash-data

:: clean stale locks
del "%DATADIR%\tensor\.lock" 2>nul
del "%DATADIR%\tensor\bitcoind.pid" 2>nul

:: start fresh wallet (no GUI to avoid conflicts)
start /B "" "%~dp0tensorcash-qt.exe" -datadir="%DATADIR%"

echo Waiting for wallet...
:wait
timeout /t 3 /nobreak >nul
curl -s -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"getblockchaininfo\"}" >nul 2>&1
if errorlevel 1 goto wait

:: create wallet
echo Creating wallet...
curl -s -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"createwallet\",\"params\":[\"main\"]}" 
echo.
timeout /t 2 /nobreak >nul

:: import key
echo Importing key...
curl -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"importprivkey\",\"params\":[\"%WIF%\"]}" 2>&1
echo.

:: save wallet.dat to desktop
for /d /r "%DATADIR%\tensor\wallets" %%d in (*) do (
  if exist "%%d\wallet.dat" (
    copy "%%d\wallet.dat" "%~dp0wallet-ready.dat"
    echo Wallet saved to %~dp0wallet-ready.dat
    echo You can now close the wallet and replace the real wallet.dat
  )
)

:: check balance
echo.
timeout /t 1 /nobreak >nul
echo Balance:
curl -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"getbalance\"}" 2>&1
echo.
pause