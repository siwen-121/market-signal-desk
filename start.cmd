@echo off
where py >nul 2>&1
if errorlevel 1 goto fallback
py -3 "%~dp0app.py" setup %*
goto done
:fallback
where python >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.10+ from https://www.python.org/downloads/
  pause
  exit /b 1
)
python "%~dp0app.py" setup %*
:done
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
