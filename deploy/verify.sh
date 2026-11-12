#!/usr/bin/env bash
#
# homebody verify: the health check install.sh and the snaphouse role run after
# a sync. It exits 0 when the uv venv can import the homebody package and, when
# the Matter controller service is active, when the controller's WebSocket
# answers on the loopback port (retried until HOMEBODY_VERIFY_DEADLINE, default
# 90 seconds, because a restarted controller is slow to listen). It never commissions anything and never touches a
# household machine; it only confirms the code imports and the controller is up.
#
# The service is often not yet running on a brand-new install (it is commissioned
# later), so a missing or inactive controller is not a failure here: only an
# active controller that does not answer is. No em dashes, no identifying data.
#
# HOMEBODY_* are overridable so the tests can drive both branches against shims.

set -euo pipefail

repo="${HOMEBODY_REPO:-/opt/homebody}"
venv="${HOMEBODY_VENV:-$repo/.venv}"
service="${HOMEBODY_MATTER_SERVICE:-homebody-matter}"
url="${HOMEBODY_MATTER_URL:-ws://127.0.0.1:5580/ws}"
py="$venv/bin/python"

# 1. The venv must be able to import the package.
if ! "$py" -c "import homebody" >/dev/null 2>&1; then
  echo "verify: the venv cannot import homebody" >&2
  exit 1
fi

# 2. If the Matter controller is running or starting, its WebSocket must answer.
# A fresh install has no controller yet, so skip the probe unless the unit is
# active or activating. After a restart the controller fetches its PAA root
# certificates before it listens (about 15 seconds on the hub), so retry the probe
# until it answers or the deadline passes; only then is it a failure.
deadline="${HOMEBODY_VERIFY_DEADLINE:-90}"
interval="${HOMEBODY_VERIFY_INTERVAL:-2}"

probe() {
  "$py" - "$url" <<'PY'
import asyncio
import sys

import websockets


async def probe(url: str) -> None:
    # The controller sends one ServerInfoMessage on connect; receiving it proves
    # the WebSocket is up and speaking the protocol.
    async with websockets.connect(url, open_timeout=5) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)


asyncio.run(probe(sys.argv[1]))
PY
}

state="$(systemctl is-active "$service" 2>/dev/null || true)"
case "$state" in
  active | activating)
    start=$SECONDS
    until probe >/dev/null 2>&1; do
      if ((SECONDS - start >= deadline)); then
        echo "verify: the Matter controller WebSocket did not answer at $url" >&2
        exit 1
      fi
      sleep "$interval"
    done
    ;;
esac

echo "verify: ok"
