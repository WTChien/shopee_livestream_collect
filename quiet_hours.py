"""Taipei-time quiet window for all automatic collectors."""

from datetime import datetime, time, timedelta
from pathlib import Path

from claim_store import TAIPEI
from settings_store import SETTINGS_PATH, load_settings


def quiet_window_end(now: datetime | None = None, *, path: Path = SETTINGS_PATH) -> datetime | None:
    settings = load_settings(path=path)
    if not settings["quiet_enabled"]:
        return None
    local = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    start = time.fromisoformat(settings["quiet_start"])
    end = time.fromisoformat(settings["quiet_end"])
    clock = local.time().replace(tzinfo=None)
    if start < end:
        return datetime.combine(local.date(), end, TAIPEI) if start <= clock < end else None
    if clock >= start:
        return datetime.combine(local.date() + timedelta(days=1), end, TAIPEI)
    if clock < end:
        return datetime.combine(local.date(), end, TAIPEI)
    return None
