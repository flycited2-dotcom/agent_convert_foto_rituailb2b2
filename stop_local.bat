@echo off
rem Stop all lanes before PC shutdown (see stop_local.py)
cd /d "%~dp0"
"C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe" stop_local.py
pause
