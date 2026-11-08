"""Saved swipe path and duration for switching live rooms on one phone."""

import hashlib
import json
import re
from pathlib import Path


SWIPE_DIR = Path(__file__).resolve().parent / "device_regions" / "swipes"
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _path(serial: str) -> Path:
    if not serial or len(serial) > 128:
        raise ValueError("手機序號不正確")
    name = serial if _SAFE.fullmatch(serial) else hashlib.sha256(serial.encode()).hexdigest()[:16]
    return SWIPE_DIR / f"{name}.json"


def load_swipe(serial: str) -> dict | None:
    try:
        data = json.loads(_path(serial).read_text(encoding="utf-8"))
        return data if data.get("serial") == serial else None
    except (OSError, ValueError, TypeError):
        return None


def save_swipe(serial: str, screen_size: list[int], start: list[int], end: list[int], duration_ms: int) -> dict:
    if (len(screen_size) != 2 or any(not isinstance(v, int) or v < 100 for v in screen_size) or
        len(start) != 2 or len(end) != 2 or
        any(not isinstance(v, int) for v in start + end)):
        raise ValueError("滑動座標或螢幕尺寸不正確")
    width, height = screen_size
    if not (0 <= start[0] < width and 0 <= end[0] < width and
            0 <= end[1] < start[1] < height and start[1] - end[1] >= height * .2):
        raise ValueError("請從畫面下方向上標記起點與終點，距離至少為螢幕高度的 20%")
    if not isinstance(duration_ms, int) or not 100 <= duration_ms <= 1500:
        raise ValueError("滑動時間請設為 100–1500 毫秒；數字越小滑得越快")
    data = {"serial": serial, "screen_size": screen_size, "start": start,
            "end": end, "duration_ms": duration_ms}
    path = _path(serial)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return data


def clear_swipe(serial: str) -> None:
    _path(serial).unlink(missing_ok=True)
