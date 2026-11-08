"""Persistent, per-claim accounting in Taiwan local time."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from settings_store import get_daily_limit


TAIPEI = timezone(timedelta(hours=8))
MAX_DAILY_CLAIMS = 90
DB_PATH = Path(__file__).resolve().parent / "data" / "claims.sqlite3"


@contextmanager
def _connect(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY,
            serial TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            claim_date TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            source TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
            status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('pending', 'confirmed')),
            confirmation_method TEXT NOT NULL DEFAULT 'ocr'
        )"""
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(claims)")}
    if "status" not in columns:
        connection.execute("ALTER TABLE claims ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")
    if "confirmation_method" not in columns:
        connection.execute("ALTER TABLE claims ADD COLUMN confirmation_method TEXT NOT NULL DEFAULT 'ocr'")
        connection.execute("UPDATE claims SET confirmation_method='manual' WHERE source='manual'")
        connection.execute("UPDATE claims SET confirmation_method='estimated' WHERE status='pending'")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_claims_date_serial ON claims(claim_date, serial)")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def amount_to_cents(value: str | float | Decimal) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金額格式不正確") from exc
    cents = amount * 100
    if amount <= 0 or amount > 10 or cents != cents.to_integral_value():
        raise ValueError("單次蝦幣金額須為 0.01～10.00，最多兩位小數")
    return int(cents)


def today() -> str:
    return datetime.now(TAIPEI).date().isoformat()


def daily_count(serial: str, day: str | None = None, *, path: Path = DB_PATH) -> int:
    day = day or today()
    with _connect(path) as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM claims WHERE serial=? AND claim_date=?", (serial, day)
        ).fetchone()[0])


def unverified_today(serial: str, *, path: Path = DB_PATH) -> bool:
    with _connect(path) as db:
        return db.execute(
            "SELECT 1 FROM claims WHERE serial=? AND claim_date=? AND status='pending' LIMIT 1",
            (serial, today()),
        ).fetchone() is not None


def record_claim(
    serial: str, amount: str | float | Decimal, *, source: str = "auto",
    day: str | None = None, path: Path = DB_PATH,
) -> int:
    if not serial or source not in ("auto", "manual"):
        raise ValueError("裝置或紀錄來源不正確")
    cents = amount_to_cents(amount)
    claim_day = date.fromisoformat(day) if day else datetime.now(TAIPEI).date()
    if claim_day > datetime.now(TAIPEI).date():
        raise ValueError("不能記錄未來日期")
    moment = datetime.now(TAIPEI) if day is None else datetime.combine(claim_day, time(12), TAIPEI)
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute(
            "SELECT COUNT(*) FROM claims WHERE serial=? AND claim_date=?",
            (serial, claim_day.isoformat()),
        ).fetchone()[0]
        limit = get_daily_limit()
        if count >= limit:
            raise ValueError(f"這台手機當日已達 {limit} 次上限")
        row = db.execute(
            "INSERT INTO claims(serial,claimed_at,claim_date,amount_cents,source,status,confirmation_method) VALUES(?,?,?,?,?,?,?)",
            (serial, moment.isoformat(), claim_day.isoformat(), cents, source, "confirmed",
             "manual" if source == "manual" else "ocr"),
        )
        return int(row.lastrowid)


def reserve_claim(serial: str, amount: str | float | Decimal, *, path: Path = DB_PATH) -> int:
    return _insert_pending(serial, amount, path=path)


def _insert_pending(serial: str, amount: str | float | Decimal, *, path: Path) -> int:
    cents = amount_to_cents(amount)
    moment = datetime.now(TAIPEI)
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute(
            "SELECT COUNT(*) FROM claims WHERE serial=? AND claim_date=?",
            (serial, moment.date().isoformat()),
        ).fetchone()[0]
        limit = get_daily_limit()
        if count >= limit:
            raise ValueError(f"這台手機當日已達 {limit} 次上限")
        row = db.execute(
            "INSERT INTO claims(serial,claimed_at,claim_date,amount_cents,source,status,confirmation_method) VALUES(?,?,?,?,?,?,?)",
            (serial, moment.isoformat(), moment.date().isoformat(), cents, "auto", "pending", "estimated"),
        )
        return int(row.lastrowid)


def confirm_claim(
    claim_id: int, amount: str | float | Decimal | None = None, *, path: Path = DB_PATH
) -> bool:
    cents = amount_to_cents(amount) if amount is not None else None
    with _connect(path) as db:
        return db.execute(
            "UPDATE claims SET status='confirmed', amount_cents=COALESCE(?,amount_cents), "
            "confirmation_method=? WHERE id=? AND status='pending'",
            (cents, "ocr" if cents is not None else "estimated", claim_id)
        ).rowcount > 0


def confirm_all_pending_estimates(*, serial: str | None = None, path: Path = DB_PATH) -> int:
    """Count previously clicked claims using the card amount when OCR confirmation was missed."""
    device_filter = " AND serial=?" if serial else ""
    device_args = (serial,) if serial else ()
    with _connect(path) as db:
        return db.execute(
            "UPDATE claims SET status='confirmed', confirmation_method='estimated' "
            "WHERE status='pending' AND source='auto'" + device_filter,
            device_args,
        ).rowcount


def cancel_claim(claim_id: int, *, path: Path = DB_PATH) -> bool:
    with _connect(path) as db:
        return db.execute(
            "DELETE FROM claims WHERE id=? AND status='pending'", (claim_id,)
        ).rowcount > 0


def delete_claim(claim_id: int, *, path: Path = DB_PATH) -> bool:
    with _connect(path) as db:
        return db.execute("DELETE FROM claims WHERE id=?", (claim_id,)).rowcount > 0


def summary(*, serial: str | None = None, path: Path = DB_PATH) -> dict:
    current_day = today()
    device_filter = " AND serial=?" if serial else ""
    device_args = (serial,) if serial else ()
    with _connect(path) as db:
        lifetime = db.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(amount_cents),0) AS cents FROM claims WHERE status='confirmed'" + device_filter,
            device_args,
        ).fetchone()
        current = db.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(amount_cents),0) AS cents FROM claims WHERE claim_date=? AND status='confirmed'" + device_filter,
            (current_day, *device_args),
        ).fetchone()
        per_device = db.execute(
            """SELECT serial, COUNT(*) AS count, SUM(amount_cents) AS cents
               FROM claims WHERE claim_date=? AND status='confirmed' GROUP BY serial ORDER BY serial""", (current_day,)
        ).fetchall()
        history = db.execute(
            """SELECT claim_date AS day, COUNT(*) AS count, SUM(amount_cents) AS cents
               FROM claims WHERE status='confirmed'""" + device_filter +
            " GROUP BY claim_date ORDER BY claim_date DESC", device_args,
        ).fetchall()
        recent = db.execute(
            """SELECT id, serial, claimed_at, claim_date AS day, amount_cents, source, status,
                      confirmation_method
               FROM claims WHERE 1=1""" + device_filter +
            " ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, claimed_at DESC, id DESC LIMIT 100",
            device_args,
        ).fetchall()
        pending = db.execute(
            "SELECT COUNT(*) FROM claims WHERE status='pending'" + device_filter,
            device_args,
        ).fetchone()[0]
        pending_by_device = db.execute(
            "SELECT serial, COUNT(*) AS count FROM claims WHERE status='pending' GROUP BY serial"
        ).fetchall()
        device_totals = db.execute(
            """SELECT serial, COUNT(*) AS count, SUM(amount_cents) AS cents,
                      SUM(CASE WHEN claim_date=? THEN 1 ELSE 0 END) AS today_count,
                      SUM(CASE WHEN claim_date=? THEN amount_cents ELSE 0 END) AS today_cents
               FROM claims WHERE status='confirmed' GROUP BY serial ORDER BY serial""",
            (current_day, current_day),
        ).fetchall()
    return {
        "today": current_day,
        "daily_limit": get_daily_limit(),
        "today_count": current["count"],
        "today_amount": current["cents"] / 100,
        "pending_count": pending,
        "pending_by_device": [dict(row) for row in pending_by_device],
        "total_count": lifetime["count"],
        "total_amount": lifetime["cents"] / 100,
        "per_device": [dict(row) | {"amount": row["cents"] / 100} for row in per_device],
        "device_totals": [dict(row) | {"amount": row["cents"] / 100,
                                       "today_amount": row["today_cents"] / 100} for row in device_totals],
        "history": [dict(row) | {"amount": row["cents"] / 100} for row in history],
        "recent": [dict(row) | {"amount": row["amount_cents"] / 100} for row in recent],
    }
