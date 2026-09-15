@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

powershell -NoProfile -Command "try { $r = Invoke-WebRequest 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
    start "" http://127.0.0.1:8000
    exit /b 0
)

echo Стройцены Бийск — http://127.0.0.1:8000
echo Подождите: браузер откроется после запуска сервера.
echo Не закрывайте это окно, пока пользуетесь сайтом.
echo.

start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($attempt = 0; $attempt -lt 60; $attempt++) { try { $r = Invoke-WebRequest 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { Start-Process 'http://127.0.0.1:8000/'; exit } } catch {}; Start-Sleep -Seconds 1 }"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo Сервер остановлен. Если выше есть ошибка, скопируйте её или сделайте снимок окна.
pause
