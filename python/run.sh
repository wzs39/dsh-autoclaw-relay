#!/usr/bin/env bash
# AutoClaw 反代网关：一键起夹具上游 + 网关 + 端到端自测
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

echo "[1/3] fixture upstream on 127.0.0.1:7890"
"$PY" fixture_upstream.py &
FIXTURE_PID=$!
trap 'kill "$FIXTURE_PID" "$RELAY_PID" 2>/dev/null || true' EXIT

sleep 1

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "config.json created from example"
fi

echo "[2/3] relay on 127.0.0.1:8080"
"$PY" relay.py -c config.json &
RELAY_PID=$!
sleep 1

echo "[3/3] end-to-end test"
set +e
"$PY" client_test.py
RC=$?
set -e

echo
echo "client_test exit code: $RC"
exit "$RC"
