@echo off
REM RAILVISION AI - start server + browser assistant on this computer (Windows)
cd /d "%~dp0"
python -c "import playwright" 2>NUL || (echo Installing Playwright (one time)... & python -m pip install playwright & python -m playwright install chromium)
python -m railvision.launch %*
pause
