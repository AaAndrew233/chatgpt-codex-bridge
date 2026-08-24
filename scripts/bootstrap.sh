#!/bin/sh
set -eu

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Codex Bridge 需要 Python 3.11 或更高版本。" >&2
  exit 2
fi

if [ ! -x "$PLUGIN_ROOT/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$PLUGIN_ROOT/.venv"
fi

"$PLUGIN_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$PLUGIN_ROOT/.venv/bin/python" -m pip install -r "$PLUGIN_ROOT/requirements.lock"

if [ ! -f "$PLUGIN_ROOT/config.json" ]; then
  cp "$PLUGIN_ROOT/config.example.json" "$PLUGIN_ROOT/config.json"
fi

if [ ! -f "$PLUGIN_ROOT/.mcp.json" ]; then
  "$PYTHON_BIN" - "$PLUGIN_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
payload = {
    "mcpServers": {
        "codex-bridge": {
            "command": str(root / "scripts" / "run_server.sh"),
            "args": [],
        }
    }
}
(root / ".mcp.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
fi

echo "Codex Bridge 初始化完成：$PLUGIN_ROOT"
echo "下一步：检查 config.json，然后运行 .venv/bin/python -m unittest discover -s tests -v"
