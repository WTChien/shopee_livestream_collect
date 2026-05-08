"""
main.py — FastAPI backend for the Shopee Live Automation Control Panel.

Run:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
Then open http://localhost:8000 in your browser.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bot_logic import ShopeeBot

# --------------------------------------------------------------------------- #
#  App setup
# --------------------------------------------------------------------------- #

app = FastAPI(title="Shopee Live Automation Panel")

# CORS 設定 - 允許前端從不同來源連線
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生產環境中應該限制為特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve index.html at root and any other static assets from the current folder.
BASE_DIR = Path(__file__).parent
DEVICES_FILE = BASE_DIR / "devices.json"

# Mount /assets for OpenCV template images, etc.
assets_dir = BASE_DIR / "assets"
assets_dir.mkdir(exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

# --------------------------------------------------------------------------- #
#  In-memory bot registry  {serial: ShopeeBot}
# --------------------------------------------------------------------------- #

_bots: Dict[str, ShopeeBot] = {}

# --------------------------------------------------------------------------- #
#  Persistence helpers
# --------------------------------------------------------------------------- #

def _load_nicknames() -> Dict[str, str]:
    """Return {serial: nickname} from devices.json."""
    if DEVICES_FILE.exists():
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("devices", {})
    return {}


def _save_nicknames(mapping: Dict[str, str]) -> None:
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump({"devices": mapping}, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
#  ADB helpers
# --------------------------------------------------------------------------- #

def _adb_devices() -> list[str]:
    """Return a list of connected ADB serial IDs (excludes 'offline')."""
    result = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True, timeout=10
    )
    serials = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if line and not line.startswith("*"):
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                serials.append(parts[0])
    return serials


# --------------------------------------------------------------------------- #
#  Request / Response models
# --------------------------------------------------------------------------- #

class RenameRequest(BaseModel):
    serial: str
    nickname: str


class ToggleRequest(BaseModel):
    serial: str


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
def serve_dashboard():
    """Serve the HTML control panel."""
    html_path = BASE_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(html_path))


@app.get("/health", include_in_schema=False)
def health_check():
    """Basic health endpoint for frontend connectivity checks."""
    return {"ok": True, "status": "healthy"}


@app.get("/devices")
def get_devices():
    """
    Return all currently connected ADB devices merged with saved nicknames
    and live bot status.
    """
    connected_serials = _adb_devices()
    nicknames = _load_nicknames()

    devices = []
    for serial in connected_serials:
        bot = _bots.get(serial)
        devices.append(
            {
                "serial":          serial,
                "nickname":        nicknames.get(serial, serial),
                "connected":       True,
                "running":         bot.running if bot else False,
                "status":          bot.status_message if bot else "idle",
                "coins_collected": bot._coins_collected if bot else 0,
                "streams_visited": bot._streams_visited if bot else 0,
            }
        )

    return {"devices": devices}


@app.post("/rename")
def rename_device(req: RenameRequest):
    """Update the custom nickname for a device in devices.json."""
    if not req.serial or not req.nickname.strip():
        raise HTTPException(status_code=400, detail="serial and nickname are required")

    nicknames = _load_nicknames()
    nicknames[req.serial] = req.nickname.strip()
    _save_nicknames(nicknames)

    # Update running bot's nickname if it exists
    if req.serial in _bots:
        _bots[req.serial].nickname = req.nickname.strip()

    return {"ok": True, "serial": req.serial, "nickname": req.nickname.strip()}


@app.post("/toggle")
def toggle_device(req: ToggleRequest):
    """Start or stop the automation bot for the given device serial."""
    connected = _adb_devices()
    if req.serial not in connected:
        raise HTTPException(status_code=404, detail=f"Device {req.serial} not connected")

    nicknames = _load_nicknames()

    if req.serial in _bots and _bots[req.serial].running:
        # Stop the bot
        _bots[req.serial].stop()
        return {"ok": True, "serial": req.serial, "running": False}
    else:
        # Start (or restart) the bot
        bot = ShopeeBot(
            serial=req.serial,
            nickname=nicknames.get(req.serial, req.serial),
        )
        _bots[req.serial] = bot
        bot.start()
        return {"ok": True, "serial": req.serial, "running": True}


@app.get("/logs/{serial}")
def get_logs(serial: str):
    """Return the last N log entries for a specific device."""
    bot = _bots.get(serial)
    if bot is None:
        return {"serial": serial, "logs": []}
    return {"serial": serial, "logs": list(bot.logs)}


@app.delete("/logs/{serial}")
def clear_logs(serial: str):
    """Clear in-memory log history for a specific device."""
    bot = _bots.get(serial)
    if bot is None:
        return {"ok": True, "serial": serial, "cleared": 0}

    cleared = len(bot.logs)
    bot.logs.clear()
    return {"ok": True, "serial": serial, "cleared": cleared}
