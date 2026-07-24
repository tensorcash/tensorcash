@echo off
title TensorCash Wallet — fast
cd /d "%~dp0"
set DATADIR=%~dp0tensorcash-data
if not exist "%DATADIR%" mkdir "%DATADIR%"
start "tensorcash-qt" "%~dp0tensorcash-qt.exe" -datadir="%DATADIR%"
