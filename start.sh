#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

choose_python() {
  for candidate in python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version=$("$candidate" -c 'import sys; print("%s.%s" % (sys.version_info[0], sys.version_info[1]))')
      major=${version%%.*}
      minor=${version#*.}
      if [[ "$major" == "3" && "$minor" -le 13 ]]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_CMD=$(choose_python || true)
if [[ -z "${PYTHON_CMD:-}" ]]; then
  echo "找不到支援的 Python 版本（建議使用 Python 3.11/3.12/3.13）。" >&2
  echo "請安裝並使用對應版本後再執行 ./start.sh" >&2
  exit 1
fi

echo "使用 Python 執行： $PYTHON_CMD"

find_free_port() {
  "$PYTHON_CMD" - <<'PY'
import socket
import sys
for port in range(8000, 8100):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', port))
            print(port)
            sys.exit(0)
        except OSError:
            continue
print('0')
sys.exit(1)
PY
}

check_port_free() {
  local port=$1
  "$PYTHON_CMD" - <<'PY'
import os
import socket
import sys
port = int(os.environ['PORT'])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    try:
        s.bind(('127.0.0.1', port))
        sys.exit(0)
    except OSError:
        sys.exit(1)
PY
}

if [[ -n "${PORT:-}" ]]; then
  export PORT
  if check_port_free "$PORT"; then
    USE_PORT="$PORT"
  else
    echo "Port $PORT 已被佔用，改為使用可用埠號。" >&2
    USE_PORT=$(find_free_port)
  fi
else
  USE_PORT=$(find_free_port)
fi

if [[ "$USE_PORT" == "0" ]]; then
  echo "找不到可用埠號，請先關閉佔用的服務或手動設定 PORT。" >&2
  exit 1
fi

check_uvicorn() {
  if "$PYTHON_CMD" -m uvicorn --help >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if ! check_uvicorn; then
  echo "錯誤：Python 環境中未安裝 uvicorn。" >&2
  echo "請先安裝相依套件：" >&2
  echo "  $PYTHON_CMD -m pip install -r requirements.txt" >&2
  echo "或啟用你的虛擬環境後再執行 ./start.sh" >&2
  exit 1
fi

echo "啟動應用程式： http://127.0.0.1:$USE_PORT"
exec "$PYTHON_CMD" -m uvicorn main:app --reload --host 0.0.0.0 --port "$USE_PORT"
