#!/bin/sh
set -e

# The live agents/ directory is a Coolify persistent volume so persona + Telegram
# rule edits survive redeploys. A *fresh* volume is empty and would hide the files
# baked into the image — so seed it once from /app/agents_seed. If the volume is
# already initialized (your edits), leave it completely untouched.
if [ ! -f /app/agents/stations.json ]; then
  echo "[entrypoint] agents/ volume is empty — seeding defaults from the image"
  mkdir -p /app/agents
  cp -r /app/agents_seed/. /app/agents/ 2>/dev/null || true
else
  echo "[entrypoint] agents/ volume already initialized — keeping your edits"
fi

exec "$@"
