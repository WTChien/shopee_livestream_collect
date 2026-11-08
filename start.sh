#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "找不到 .venv，請先依 README 安裝專案環境。" >&2
  exit 1
fi
exec .venv/bin/python local_app.py
