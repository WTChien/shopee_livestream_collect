"""Persistent settings shared by dashboard and scheduled collectors."""

import json
import os
import re
import tempfile
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path


SETTINGS_PATH = Path(__file__).resolve().parent / "data" / "settings.json"
DEFAULT_MIN_REWARD = Decimal("0.30")
DEFAULT_MISSING_CARD_LIMIT = 20
DEFAULT_DAILY_LIMIT = 90
DEFAULT_READ_INTERVAL_SECONDS = 5
DEFAULT_LIVE_REFRESH_MINUTES = 30
DEFAULT_LIMIT_STREAK = 5
DEFAULT_SUCCESS_CAPTURE_SECONDS = 8
DEFAULT_QUIET_START = "23:30"
DEFAULT_QUIET_END = "00:30"
_settings_lock = threading.Lock()
_time_re = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_integer_settings = {
    "daily_limit": (1, 99, DEFAULT_DAILY_LIMIT),
    "read_interval_seconds": (1, 60, DEFAULT_READ_INTERVAL_SECONDS),
    "live_refresh_minutes": (1, 120, DEFAULT_LIVE_REFRESH_MINUTES),
    "limit_streak": (1, 20, DEFAULT_LIMIT_STREAK),
    "success_capture_seconds": (2, 15, DEFAULT_SUCCESS_CAPTURE_SECONDS),
}


def validate_min_reward(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or amount < 0 or amount > 10 or cents != cents.to_integral_value():
            raise ValueError
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("最低蝦幣須為 0.00～10.00，最多兩位小數") from exc
    return amount


def validate_missing_card_limit(value: object) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 100:
        raise ValueError("連續無卡片直播間數須為 1～100 的整數")
    return int(value)


def validate_integer_setting(name: str, value: object) -> int:
    minimum, maximum, _ = _integer_settings[name]
    if isinstance(value, bool) or not str(value).isdigit() or not minimum <= int(value) <= maximum:
        raise ValueError(f"{name} 須為 {minimum}～{maximum} 的整數")
    return int(value)


def validate_quiet_time(value: object) -> str:
    if not isinstance(value, str) or not _time_re.fullmatch(value):
        raise ValueError("暫停時段須使用 HH:MM 格式")
    return value


def load_settings(*, path: Path = SETTINGS_PATH) -> dict:
    if not path.exists():
        return {"min_reward": DEFAULT_MIN_REWARD,
                "missing_card_limit": DEFAULT_MISSING_CARD_LIMIT,
                **{name: default for name, (_, _, default) in _integer_settings.items()},
                "quiet_enabled": True, "quiet_start": DEFAULT_QUIET_START,
                "quiet_end": DEFAULT_QUIET_END}
    with path.open("r", encoding="utf-8") as source:
        data = json.load(source)
    start = validate_quiet_time(data.get("quiet_start", DEFAULT_QUIET_START))
    end = validate_quiet_time(data.get("quiet_end", DEFAULT_QUIET_END))
    if start == end:
        raise ValueError("暫停開始與結束時間不可相同")
    return {"min_reward": validate_min_reward(data.get("min_reward", DEFAULT_MIN_REWARD)),
            "missing_card_limit": validate_missing_card_limit(
                data.get("missing_card_limit", DEFAULT_MISSING_CARD_LIMIT)),
            **{name: validate_integer_setting(name, data.get(name, default))
               for name, (_, _, default) in _integer_settings.items()},
            "quiet_enabled": bool(data.get("quiet_enabled", True)),
            "quiet_start": start, "quiet_end": end}


def get_min_reward(*, path: Path = SETTINGS_PATH) -> Decimal:
    return load_settings(path=path)["min_reward"]


def get_missing_card_limit(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["missing_card_limit"]


def get_daily_limit(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["daily_limit"]


def get_read_interval(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["read_interval_seconds"]


def get_live_refresh_minutes(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["live_refresh_minutes"]


def get_limit_streak(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["limit_streak"]


def get_success_capture_seconds(*, path: Path = SETTINGS_PATH) -> int:
    return load_settings(path=path)["success_capture_seconds"]


def save_settings(*, min_reward: object | None = None,
                  missing_card_limit: object | None = None,
                  daily_limit: object | None = None,
                  read_interval_seconds: object | None = None,
                  live_refresh_minutes: object | None = None,
                  limit_streak: object | None = None,
                  success_capture_seconds: object | None = None,
                  quiet_enabled: bool | None = None,
                  quiet_start: str | None = None,
                  quiet_end: str | None = None,
                  path: Path = SETTINGS_PATH) -> dict:
    # Validate before touching the existing file.
    amount = validate_min_reward(min_reward) if min_reward is not None else None
    limit = validate_missing_card_limit(missing_card_limit) if missing_card_limit is not None else None
    integer_updates = {
        name: validate_integer_setting(name, value)
        for name, value in {
            "daily_limit": daily_limit,
            "read_interval_seconds": read_interval_seconds,
            "live_refresh_minutes": live_refresh_minutes,
            "limit_streak": limit_streak,
            "success_capture_seconds": success_capture_seconds,
        }.items() if value is not None
    }
    start = validate_quiet_time(quiet_start) if quiet_start is not None else None
    end = validate_quiet_time(quiet_end) if quiet_end is not None else None
    if quiet_enabled is not None and not isinstance(quiet_enabled, bool):
        raise ValueError("暫停時段啟用狀態不正確")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _settings_lock:
        current = load_settings(path=path)
        if amount is not None:
            current["min_reward"] = amount
        if limit is not None:
            current["missing_card_limit"] = limit
        current.update(integer_updates)
        if quiet_enabled is not None:
            current["quiet_enabled"] = quiet_enabled
        if start is not None:
            current["quiet_start"] = start
        if end is not None:
            current["quiet_end"] = end
        if current["quiet_start"] == current["quiet_end"]:
            raise ValueError("暫停開始與結束時間不可相同")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as target:
                temp_path = Path(target.name)
                json.dump({"min_reward": f"{current['min_reward']:.2f}",
                           "missing_card_limit": current["missing_card_limit"],
                           **{name: current[name] for name in _integer_settings},
                           "quiet_enabled": current["quiet_enabled"],
                           "quiet_start": current["quiet_start"],
                           "quiet_end": current["quiet_end"]}, target, ensure_ascii=False)
                target.write("\n")
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return current


def save_min_reward(value: object, *, path: Path = SETTINGS_PATH) -> Decimal:
    return save_settings(min_reward=value, path=path)["min_reward"]


def save_missing_card_limit(value: object, *, path: Path = SETTINGS_PATH) -> int:
    return save_settings(missing_card_limit=value, path=path)["missing_card_limit"]
