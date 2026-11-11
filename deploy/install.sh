#!/usr/bin/env bash
#
# homebody installer, run as root by the snaphouse homebody_bootstrap role on
# first install and, through deploy/update.sh, by the update timer. Every step is
# idempotent and safe to re-run. It operates on the checkout already at
# /opt/homebody (update.sh does the git fetch and checkout); install.sh ensures
# the service user, the local config, the working directories, and the uv venv,
# then runs deploy/verify.sh and records the current commit as last-good.
#
# Python is managed with uv, which the snaphouse common role has already put on
# the hub. No pip, no apt python packages. No em dashes, no identifying data:
# nothing here carries a name, address, MAC, IP, or pairing code; those live only
# in the untracked /etc/homebody/local.toml this script seeds from the example.
#
# HOMEBODY_* are overridable so the test suite can drive individual functions.

set -euo pipefail

HOMEBODY_REPO="${HOMEBODY_REPO:-/opt/homebody}"
HOMEBODY_CONFIG_DIR="${HOMEBODY_CONFIG_DIR:-/etc/homebody}"
HOMEBODY_STATE_ROOT="${HOMEBODY_STATE_ROOT:-/var/lib/homebody}"
HOMEBODY_STATE="${HOMEBODY_STATE:-$HOMEBODY_STATE_ROOT/update/update.state}"
HOMEBODY_USER="${HOMEBODY_USER:-homebody}"
HOMEBODY_VERIFY="${HOMEBODY_VERIFY:-$HOMEBODY_REPO/deploy/verify.sh}"
# Keep uv's managed Python in a shared, world-readable location (matching
# snaphouse and nightstand), so the unprivileged homebody service user can exec
# the venv interpreter rather than one under root's private uv directory.
UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"
export UV_PYTHON_INSTALL_DIR

log() {
  printf '==> %s\n' "$*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root" >&2
    exit 1
  fi
}

create_user() {
  if ! id -u "$HOMEBODY_USER" >/dev/null 2>&1; then
    log "Creating the $HOMEBODY_USER system user"
    useradd --system --home-dir "$HOMEBODY_STATE_ROOT" \
      --shell /usr/sbin/nologin "$HOMEBODY_USER"
  fi
}

seed_config() {
  mkdir -p "$HOMEBODY_CONFIG_DIR"
  local local_toml="$HOMEBODY_CONFIG_DIR/local.toml"
  if [ ! -f "$local_toml" ]; then
    log "Seeding $local_toml from the example"
    cp "$HOMEBODY_REPO/local.example.toml" "$local_toml"
  fi
  # The local config holds the only identifying data on the hub (phone
  # addresses, the Nest's node id). Keep it readable by root and the service
  # group only, never world-readable.
  chmod 0640 "$local_toml"
  chown "root:$HOMEBODY_USER" "$local_toml"
}

make_dirs() {
  log "Creating the working directories under $HOMEBODY_STATE_ROOT"
  mkdir -p "$HOMEBODY_STATE_ROOT"
  chown "root:root" "$HOMEBODY_STATE_ROOT"
  chmod 0755 "$HOMEBODY_STATE_ROOT"

  # The service user owns its state and the Matter controller's storage.
  for sub in state matter; do
    mkdir -p "$HOMEBODY_STATE_ROOT/$sub"
    chown "$HOMEBODY_USER:$HOMEBODY_USER" "$HOMEBODY_STATE_ROOT/$sub"
    chmod 0750 "$HOMEBODY_STATE_ROOT/$sub"
  done

  # The self-update state must be writable only by root, so an unprivileged user
  # cannot steer which commit the updater keeps or trust a tampered state file.
  local update_dir
  update_dir="$(dirname "$HOMEBODY_STATE")"
  mkdir -p "$update_dir"
  chown "root:root" "$update_dir"
  chmod 0700 "$update_dir"
}

sync_venv() {
  log "Syncing the uv environment (frozen, no dev)"
  ( cd "$HOMEBODY_REPO" && uv sync --frozen --no-dev )
}

run_verify() {
  log "Verifying the install"
  bash "$HOMEBODY_VERIFY"
}

# Record the current commit as the last-good commit, preserving the failed line
# (line 2) the updater may have written. Written atomically at mode 0600 in the
# root-owned update directory.
record_last_good() {
  local commit tmp failed
  commit="$(git -C "$HOMEBODY_REPO" rev-parse HEAD)"
  mkdir -p "$(dirname "$HOMEBODY_STATE")"
  failed="$(sed -n '2p' "$HOMEBODY_STATE" 2>/dev/null || true)"
  tmp="$(mktemp "$(dirname "$HOMEBODY_STATE")/.hb.XXXXXX")"
  printf '%s\n%s\n' "$commit" "$failed" >"$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$HOMEBODY_STATE"
}

main() {
  require_root
  create_user
  seed_config
  make_dirs
  sync_venv
  run_verify
  record_last_good
  log "homebody install complete"
}

# Run only when executed, not when sourced (the tests source this file to drive
# individual functions with shimmed commands).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
