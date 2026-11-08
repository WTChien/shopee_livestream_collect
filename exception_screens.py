"""Per-phone visual signatures for screens that interrupt a live room."""

import base64
import hashlib
import json
import re
from pathlib import Path

import cv2
import numpy as np


BASE_DIR = Path(__file__).resolve().parent / "device_regions" / "exceptions"
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MATCH_THRESHOLD = 0.83


def _directory(serial: str) -> Path:
    if not serial or len(serial) > 128:
        raise ValueError("手機序號不正確")
    name = serial if _SAFE.fullmatch(serial) else hashlib.sha256(serial.encode()).hexdigest()[:16]
    return BASE_DIR / name


def list_profiles(serial: str) -> list[dict]:
    path = _directory(serial) / "profiles.json"
    try:
        profiles = json.loads(path.read_text(encoding="utf-8"))
        return profiles if isinstance(profiles, list) else []
    except (OSError, ValueError):
        return []


def save_profile(serial: str, profile: dict, templates: list[str]) -> dict:
    name = str(profile.get("name", "")).strip()
    boxes = profile.get("boxes")
    size = profile.get("screen_size")
    action = profile.get("action")
    point = profile.get("action_point")
    if not name or len(name) > 30 or not isinstance(size, list) or len(size) != 2 or any(
        not isinstance(v, int) or v < 100 for v in size
    ):
        raise ValueError("請輸入畫面名稱與正確的螢幕尺寸")
    width, height = size
    if not isinstance(boxes, list) or not 1 <= len(boxes) <= 3 or len(templates) != len(boxes):
        raise ValueError("請框選 1 至 3 個特徵區塊")
    for box in boxes:
        if (not isinstance(box, list) or len(box) != 4 or
            any(not isinstance(v, int) for v in box) or
            not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height) or
            box[2] - box[0] < 20 or box[3] - box[1] < 20):
            raise ValueError("特徵框需在畫面內，且至少 20 × 20 像素")
    if action not in ("back", "tap"):
        raise ValueError("請選擇返回鍵或點擊位置")
    if action == "tap" and (not isinstance(point, list) or len(point) != 2 or
                            any(not isinstance(v, int) for v in point) or
                            not (0 <= point[0] < width and 0 <= point[1] < height)):
        raise ValueError("請在畫面上指定關閉按鈕位置")
    directory = _directory(serial)
    profiles = list_profiles(serial)
    existing = next((item for item in profiles if item["name"] == name), None)
    profile_id = existing["id"] if existing else hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    decoded = []
    for box, encoded in zip(boxes, templates):
        try:
            raw = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        except (ValueError, TypeError):
            image = None
        if image is None or image.shape != (box[3] - box[1], box[2] - box[0]):
            raise ValueError("特徵圖片與框選範圍不符，請重新擷取畫面")
        if image.std() < 8:
            raise ValueError("特徵區塊變化太少，請框選有文字或圖示的區域")
        decoded.append(image)
    directory.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(decoded):
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("無法儲存特徵圖片")
        (directory / f"{profile_id}-{index}.png").write_bytes(encoded.tobytes())
    for index in range(len(decoded), 3):
        (directory / f"{profile_id}-{index}.png").unlink(missing_ok=True)
    saved = {"id": profile_id, "name": name, "screen_size": size, "boxes": boxes,
             "action": action, "action_point": point if action == "tap" else None}
    profiles = [item for item in profiles if item["id"] != profile_id]
    profiles.append(saved)
    temporary = directory / "profiles.tmp"
    temporary.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "profiles.json")
    return saved


def delete_profile(serial: str, profile_id: str) -> bool:
    directory = _directory(serial)
    profiles = list_profiles(serial)
    retained = [item for item in profiles if item["id"] != profile_id]
    if len(retained) == len(profiles):
        return False
    temporary = directory / "profiles.tmp"
    temporary.write_text(json.dumps(retained, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "profiles.json")
    for index in range(3):
        (directory / f"{profile_id}-{index}.png").unlink(missing_ok=True)
    return True


def match_profile(screen: np.ndarray, serial: str) -> dict | None:
    height, width = screen.shape[:2]
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    for profile in list_profiles(serial):
        old_width, old_height = profile["screen_size"]
        scores = []
        for index, box in enumerate(profile["boxes"]):
            path = _directory(serial) / f"{profile['id']}-{index}.png"
            try:
                template = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
            except (OSError, cv2.error):
                template = None
            if template is None:
                break
            x1, y1, x2, y2 = [round(value * (width if i % 2 == 0 else height) /
                                    (old_width if i % 2 == 0 else old_height)) for i, value in enumerate(box)]
            expected_width, expected_height = x2 - x1, y2 - y1
            if expected_width < 1 or expected_height < 1:
                break
            if template.shape != (expected_height, expected_width):
                template = cv2.resize(template, (expected_width, expected_height))
            pad_x, pad_y = max(12, width // 40), max(12, height // 40)
            search = gray[max(0, y1-pad_y):min(height, y2+pad_y),
                          max(0, x1-pad_x):min(width, x2+pad_x)]
            if search.shape[0] < expected_height or search.shape[1] < expected_width:
                break
            score = float(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED).max())
            if score < MATCH_THRESHOLD:
                break
            scores.append(score)
        if len(scores) == len(profile["boxes"]):
            return {**profile, "score": round(min(scores), 3)}
    return None
