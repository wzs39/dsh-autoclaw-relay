@echo off
setlocal
cd /d "%~dp0"

set PY=python
where python >nul 2>nul || set PY=py

echo [1/3] starting fixture upstream on 127.0.0.1:7890
start "autoclaw-fixture" /min "%PY%" fixture_upstream.py

timeout /t 2 /nobreak >nul

if not exist config.json (
  copy config.example.json config.json >nul
  echo config.json created from example
)

echo [2/3] starting relay on 127.0.0.1:8080
start "autoclaw-relay" /min "%PY%" relay.py -c config.json

timeout /t 2 /nobreak >nul

echo [3/3] running end-to-end test
"%PY%" client_test.py
set RC=%ERRORLEVEL%

echo.
echo client_test exit code: %RC%
exit /b %RC%
