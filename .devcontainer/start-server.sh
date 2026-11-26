#!/usr/bin/env bash
# Starts the Trade Hour server detached from the Codespaces lifecycle shell,
# so it keeps running after this script exits. Safe to run more than once.
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] || cp .env.example .env
python -c "import cryptography, requests" 2>/dev/null || pip install -q -r requirements.txt
if pgrep -f "python api/server.py" >/dev/null; then
  echo "Trade Hour server already running"
  exit 0
fi
HOST=0.0.0.0 setsid nohup python api/server.py > /tmp/server.log 2>&1 < /dev/null &
echo "Trade Hour server started on port ${PORT:-8787} (log: /tmp/server.log)"
