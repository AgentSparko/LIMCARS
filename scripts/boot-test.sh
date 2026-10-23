#!/usr/bin/env bash
# boot-test for the clean LIMCARS copy: start on a spare port, curl, kill
set -u
cd "$(dirname "$0")/.."
LIMCARS_PORT=8999 /usr/bin/python3 -m app > /tmp/limcars-boot.log 2>&1 &
BPID=$!
sleep 4
echo "health: $(curl -s -m 3 http://127.0.0.1:8999/api/health)"
echo "dash:   $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8999/)"
kill "$BPID" 2>/dev/null
wait "$BPID" 2>/dev/null
tail -2 /tmp/limcars-boot.log
