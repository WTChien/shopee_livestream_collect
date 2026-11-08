"""Run one scheduled collection session independently of the dashboard window."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from bot_logic import ShopeeBot, close_shopee
from foreground_app import foreground_app
from claim_store import TAIPEI, daily_count
from settings_store import get_daily_limit
from daily_schedule import get_schedule


DATA_DIR = Path(__file__).resolve().parent / "data"
STATUS_DIR = DATA_DIR / "daily_status"


def _status_path(serial: str, suffix: str) -> Path:
    name = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:20]
    return STATUS_DIR / f"{name}.{suffix}"


def request_stop(serial: str) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    _status_path(serial, "stop").write_text("stop\n", encoding="ascii")


def write_status(serial: str, state: str, message: str) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = _status_path(serial, "json")
    data = {
        "serial": serial, "state": state, "message": message,
        "pid": os.getpid(), "updated_at": datetime.now(TAIPEI).isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_status(serial: str) -> dict | None:
    path = _status_path(serial, "json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(data["updated_at"])
        if data.get("state") == "running" and (datetime.now(TAIPEI) - stamp).total_seconds() > 30:
            data["state"] = "stale"
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    serial = args.serial
    config = get_schedule(serial)
    stop_file = _status_path(serial, "stop")
    stop_file.unlink(missing_ok=True)
    if not config["enabled"]:
        write_status(serial, "disabled", "每日排程已停用")
        return 0
    log_dir = DATA_DIR / "schedule_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / f"{serial}-{datetime.now(TAIPEI):%Y-%m-%d}.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    daily_limit = get_daily_limit()
    if daily_count(serial) >= daily_limit:
        write_status(serial, "done", f"今日已達 {daily_limit} 次，無需啟動")
        return 0
    bot = ShopeeBot(
        serial,
        idle_timeout_seconds=config["idle_minutes"] * 60 if config["completion_mode"] == "idle" else None,
        max_runtime_seconds=(config["max_runtime_minutes"] * 60
                             if config["completion_mode"] == "idle" else None),
        launch_on_start=True,
        guard_foreground=True,
    )
    try:
        bot.start()
    except RuntimeError as exc:
        write_status(serial, "already_running", str(exc))
        return 0
    try:
        while bot.running:
            if stop_file.exists():
                bot.stop()
                write_status(serial, "stopped", "已從介面停止每日排程")
                return 0
            write_status(serial, "running", bot.status_message)
            time.sleep(5)
        if bot._thread:
            bot._thread.join(timeout=20)
        state = "error" if bot.status_message.startswith("❌") else "done"
        write_status(serial, state, bot.status_message)
        return 0
    except KeyboardInterrupt:
        bot.stop()
        write_status(serial, "stopped", "排程已手動停止")
        return 130
    finally:
        stop_file.unlink(missing_ok=True)
        if foreground_app(serial)["kind"] == "shopee":
            close_shopee(serial)


if __name__ == "__main__":
    raise SystemExit(main())
