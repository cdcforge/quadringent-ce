#!/usr/bin/env sh
# Les identités de démonstration sont fictives, aucun accès externe.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
set -a
. "$repo_root/examples/local.env"
set +a
exec "$repo_root/.venv/bin/quadringent-control-plane" --ui-dist "$repo_root/ui/dist" \
  --state-dir "$repo_root/.local-state" --port "${QUADRINGENT_LOCAL_PORT:-8844}" "$@"
