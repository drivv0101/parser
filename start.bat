@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo Стройцены Бийск — http://127.0.0.1:8000
echo Если база старше 12 часов, обход магазинов начнётся сам. Закрыть окно = остановить сайт.
echo.

start "" http://127.0.0.1:8000
"%PY%" -m uvicorn app.main:app --port 8000
