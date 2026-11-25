@echo off
setlocal
cd /d "%~dp0"
python sb50_learn.py %*
endlocal
