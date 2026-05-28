#!/bin/bash
# Idempotent bench restart — kills old, waits for port free, loads new, verifies.
# Fixes the double-server-on-8001 race that's caused phantom 404s all day.
set -e
PLIST="$HOME/Library/LaunchAgents/com.thespacepit.dashboard.plist"

echo "→ unloading launchd"
launchctl unload "$PLIST" 2>/dev/null || true

echo "→ killing any straggler python servers"
pkill -9 -f "tools/dashboard/server.py" 2>/dev/null || true

echo "→ waiting for port 8001 to free"
for i in {1..10}; do
  if ! lsof -iTCP:8001 -sTCP:LISTEN -n -P > /dev/null 2>&1; then
    echo "  ✓ port 8001 free after ${i}s"
    break
  fi
  sleep 1
done

if lsof -iTCP:8001 -sTCP:LISTEN -n -P > /dev/null 2>&1; then
  echo "  ✗ port 8001 still occupied after 10s — manual intervention needed:"
  lsof -iTCP:8001 -sTCP:LISTEN -n -P
  exit 1
fi

echo "→ loading launchd"
launchctl load "$PLIST"

echo "→ waiting for server to respond"
for i in {1..10}; do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8001/ 2>/dev/null)" = "200" ]; then
    echo "  ✓ bench up after ${i}s"
    break
  fi
  sleep 1
done

PROC_COUNT=$(lsof -iTCP:8001 -sTCP:LISTEN -n -P | tail -n +2 | wc -l | tr -d ' ')
if [ "$PROC_COUNT" != "1" ]; then
  echo "  ✗ expected 1 server, got ${PROC_COUNT}:"
  lsof -iTCP:8001 -sTCP:LISTEN -n -P
  exit 1
fi

echo "✓ bench restarted clean. one process. ready."
