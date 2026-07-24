@echo off
cd /d "%~dp0"
del tensorcash-data\tensor\.lock tensorcash-data\tensor\bitcoind.pid 2>nul
taskkill /f /im tensorcash-qt.exe 2>nul
start /B "" tensorcash-qt.exe -datadir="%cd%\tensorcash-data"
set /p WIF=Enter WIF private key: 
py tensorcash_import_key.py --wif "%WIF%" --wallet "main" --backup "%cd%\tensorcash-recovered-wallet.dat" --timestamp 0 --explicit-rescan
pause
