"""Read the visible Android app through ADB without changing device state."""

import re
import subprocess


SHOPEE_PACKAGE = "com.shopee.tw"
_RESUMED = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity):?\s+ActivityRecord\{[^}]*?\s([\w.]+)/")
_COMPONENT = re.compile(r"^([\w.]+)/[\w.$]+$", re.MULTILINE)


def _shell(serial: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["adb", "-s", serial, "shell", *args],
                                capture_output=True, text=True, errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def foreground_app(serial: str) -> dict:
    """Return package and kind: shopee, home, other, or unknown."""
    activities = _shell(serial, "dumpsys", "activity", "activities")
    match = _RESUMED.search(activities or "")
    if not match:
        return {"package": None, "kind": "unknown"}
    package = match.group(1)
    if package == SHOPEE_PACKAGE:
        kind = "shopee"
    else:
        home = _shell(serial, "cmd", "package", "resolve-activity", "--brief",
                      "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME")
        home_match = _COMPONENT.search(home or "")
        kind = "home" if home_match and package == home_match.group(1) else "other"
    return {"package": package, "kind": kind}
