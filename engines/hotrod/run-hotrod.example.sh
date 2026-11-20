#!/usr/bin/env bash
# Starts Strata-HotRod's server for this kit's gateway (docs/engines/hotrod.md). Linux only. UNTESTED with this kit:
# the command is the one from Strata-HotRod's README, with the port, host and key file made into variables.
#
# Copy it next to your Strata-HotRod checkout, set the three variables, and run it (or wrap it in a systemd unit).
# The key file holds one line: a long random key. Put the same key in the kit's backendKeyFile on the Windows PC.
set -euo pipefail

HOTROD_DIR="${HOTROD_DIR:-$HOME/Strata-HotRod}"          # the clone that ran setup.sh --build
CONFIG="${CONFIG:-strata-iq3_s.json}"                    # the config setup.sh wrote (relative to HOTROD_DIR)
PORT="${PORT:-8095}"
KEY_FILE="${KEY_FILE:-$HOME/.config/hotrod/api-key}"     # chmod 600; never commit it

cd "$HOTROD_DIR"
# Their server also reads the key from $STRATA_API_KEY, which keeps it off the process list.
STRATA_API_KEY="$(head -n1 "$KEY_FILE")"
export STRATA_API_KEY

# Strata-HotRod's README: run inside a memory limit of 94% of RAM ("two uncapped runs once froze this PC").
# This is their command as written. systemd-run --scope may ask for privileges; `systemd-run --user --scope` works
# where the user manager has the memory controller delegated (untested here). A scope runs the command as a child of
# this shell, so the exported key reaches it.
exec systemd-run --scope -p MemoryMax="$(awk '/MemTotal/ {printf "%dK", $2 * 0.94}' /proc/meminfo)" -p MemorySwapMax=0 \
  .venv/bin/python serve/server.py --engine strata --config "$CONFIG" --port "$PORT" --host 0.0.0.0
