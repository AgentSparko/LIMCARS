#!/usr/bin/env bash
# Start LIMCARS-NEXT in the foreground (used by the systemd unit and by hand).
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${LIMCARS_PYTHON:-python3}" -m app
