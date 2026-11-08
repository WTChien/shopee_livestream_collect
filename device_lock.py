"""One active collector per ADB device, even across dashboard and scheduled runs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


LOCK_DIR = Path(__file__).resolve().parent / "data" / "locks"


def acquire_device_lock(serial: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:20]
    handle = (LOCK_DIR / f"{name}.lock").open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(f"{serial} 已有自動領取流程在執行") from exc
    return handle


def release_device_lock(handle) -> None:
    if handle is None or handle.closed:
        return
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
