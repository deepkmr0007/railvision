#!/usr/bin/env sh
# RAILVISION AI — start server + browser assistant on this computer (Linux / macOS)
cd "$(dirname "$0")"
python3 -c "import playwright" 2>/dev/null || { echo "Installing Playwright (one time)…"; python3 -m pip install --user playwright && python3 -m playwright install chromium; }
exec python3 -m railvision.launch "$@"
