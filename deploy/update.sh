#!/usr/bin/env bash
#
# homebody self-update, run by the homebody-update timer. It fetches main,
# checks it out, re-runs the installer (which syncs the venv, verifies, and
# records last-good), and keeps the newest commit of main that passes verify,
# never getting stuck on a broken commit:
#
#   git fetch origin main        (on failure: change nothing, exit 0)
#   target = origin/main
#   if target is the running commit, or a commit already known to fail here:
#       exit 0
#   check out target; run install.sh
#   pass: target becomes last_good, restart the services, exit 0
#   fail: record target as failed, check out last_good, re-install, restart
#
# State lives in HOMEBODY_STATE as two lines, last_good then failed (either may
# be empty). It is trusted only when its directory and the file are root-owned,
# so a tampered file cannot steer a checkout. No em dashes, no identifying data.
#
# HOMEBODY_* are overridable so the tests can drive the whole algorithm against a
# temp repo with a shimmed installer.

set -euo pipefail

repo="${HOMEBODY_REPO:-/opt/homebody}"
state="${HOMEBODY_STATE:-/var/lib/homebody/update/update.state}"
install_cmd="${HOMEBODY_INSTALL:-$repo/deploy/install.sh}"
services="${HOMEBODY_SERVICES:-homebody-matter}"

cd "$repo"

# Only a full git object id is a trusted commit; validating what we read keeps a
# tampered state file from steering a checkout.
is_sha() {
  printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$'
}

# Trust the state file only when the updater owns its directory and the file.
state_dir="$(dirname "$state")"
last_good=""
failed=""
if [ -f "$state" ] && [ -O "$state_dir" ] && [ -O "$state" ]; then
  last_good="$(sed -n '1p' "$state")"
  failed="$(sed -n '2p' "$state")"
fi
is_sha "$last_good" || last_good=""
is_sha "$failed" || failed=""

write_state() {
  local tmp
  mkdir -p "$state_dir"
  tmp="$(mktemp "$state_dir/.hb.XXXXXX")"
  printf '%s\n%s\n' "$last_good" "$failed" >"$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$state"
}

restart_services() {
  local svc
  for svc in $services; do
    systemctl restart "$svc" || true
  done
}

# A failed fetch (no network, origin unreachable) must change nothing.
if ! git fetch origin main >/dev/null 2>&1; then
  exit 0
fi

target="$(git rev-parse origin/main)"
head="$(git rev-parse HEAD)"

# Nothing to do when main is already the running commit, or a commit already
# seen to fail here (do not retry it until main moves).
if [ "$target" = "$head" ] || { [ -n "$failed" ] && [ "$target" = "$failed" ]; }; then
  exit 0
fi

# Try the target: check it out and run the installer (sync, verify, last-good).
git checkout --quiet "$target"
if "$install_cmd"; then
  last_good="$target"
  failed=""
  write_state
  restart_services
  exit 0
fi

# The target failed verify. Record it so it is not retried until main moves.
failed="$target"
write_state

# With no earlier good commit there is nothing to fall back to.
if [ -z "$last_good" ]; then
  exit 1
fi

# Fall back to the last good commit, re-install, and restart. Exit 0 when the
# fallback is healthy (the machine is fine, just not on the newest commit), so
# the timer does not flap on a bad upstream commit; exit 1 only if even the
# fallback fails to install.
git checkout --quiet "$last_good"
if "$install_cmd"; then
  restart_services
  exit 0
fi
restart_services
exit 1
