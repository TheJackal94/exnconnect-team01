#!/usr/bin/env bash
# Starts the Trade Hour server on every Codespace start. `setsid -f` fully
# detaches it so it survives the lifecycle shell exiting. Safe to run twice.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
[ -f .env ] || cp .env.example .env
python -c "import cryptography, requests" 2>/dev/null || pip install -q -r requirements.txt
if pgrep -f "[p]ython api/server.py" >/dev/null; then
  echo "Trade Hour server already running"
  exit 0
fi
HOST=0.0.0.0 setsid -f nohup python api/server.py > data/server.log 2>&1 < /dev/null
sleep 2
if pgrep -f "[p]ython api/server.py" >/dev/null; then
  echo "Trade Hour server running on port ${PORT:-8787} (log: data/server.log)"
else
  echo "Trade Hour server failed to start; see data/server.log" >&2
  tail -20 data/server.log >&2
fi
