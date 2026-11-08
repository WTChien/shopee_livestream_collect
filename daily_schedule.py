"""Per-device daily collection schedule and Windows Task Scheduler integration."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "data" / "daily_schedule.json"
TASK_DIR = BASE_DIR / "data" / "tasks"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_MAX_RUNTIME_MINUTES = 240
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def task_name(serial: str) -> str:
    if not _SERIAL_RE.fullmatch(serial):
        raise ValueError("ADB 裝置序號格式不正確")
    digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
    return f"ShopeeLiveCollect_{digest}"


def get_schedule(serial: str) -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            entry = saved.get("devices", {}).get(serial, {})
        except (OSError, ValueError, AttributeError):
            entry = {}
    else:
        entry = {}
    return {
        "serial": serial,
        "enabled": bool(entry.get("enabled", False)),
        "time": entry.get("time", "08:00"),
        "completion_mode": entry.get("completion_mode", "idle"),
        "idle_minutes": int(entry.get("idle_minutes", DEFAULT_IDLE_MINUTES)),
        "max_runtime_minutes": int(entry.get("max_runtime_minutes", DEFAULT_MAX_RUNTIME_MINUTES)),
    }


def save_schedule(serial: str, *, enabled: bool, time: str,
                  idle_minutes: int, max_runtime_minutes: int,
                  completion_mode: str = "idle") -> dict:
    match = _TIME_RE.fullmatch(time.strip())
    if not match or not 5 <= idle_minutes <= 240 or not 30 <= max_runtime_minutes <= 720 or completion_mode not in ("idle", "daily_limit"):
        raise ValueError("排程時間或結束條件超出範圍")
    normalized = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    entry = {
        "enabled": enabled,
        "time": normalized,
        "completion_mode": completion_mode,
        "idle_minutes": idle_minutes,
        "max_runtime_minutes": max_runtime_minutes,
    }
    try:
        all_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {"devices": {}}
    except (OSError, ValueError):
        all_data = {"devices": {}}
    all_data.setdefault("devices", {})[serial] = entry
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(all_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_FILE)
    return {"serial": serial, **entry}


def _task_script(serial: str) -> Path:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    path = TASK_DIR / f"{task_name(serial)}.cmd"
    # %~dp0 is the task directory. The project root is two levels above it.
    path.write_bytes((
        '@echo off\r\n'
        'cd /d "%~dp0..\\.."\r\n'
        f'".venv\\Scripts\\python.exe" daily_run.py --serial "{serial}"\r\n'
    ).encode("ascii"))
    return path


def install_task(serial: str, time: str) -> None:
    script = _task_script(serial)
    result = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", time,
         "/TN", task_name(serial), "/TR", f'"{script}"'],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Windows 工作排程建立失敗")


def remove_task(serial: str) -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", task_name(serial)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 and "cannot find" not in (result.stderr + result.stdout).lower():
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Windows 工作排程移除失敗")
