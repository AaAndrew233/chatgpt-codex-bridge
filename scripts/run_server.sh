#!/bin/sh
set -eu

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN="$PLUGIN_ROOT/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Codex Bridge 尚未初始化，请先运行 $PLUGIN_ROOT/scripts/bootstrap.sh" >&2
  exit 2
fi

exec "$PYTHON_BIN" "$PLUGIN_ROOT/server.py" "$@"
