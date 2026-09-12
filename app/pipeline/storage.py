"""Block 9 - Persistence.

Local backend: SQLite for the structured JSON, a folder for receipt images and PDFs.
The interface is a single save()/list()/get() trio so an S3 + DynamoDB backend can be
swapped in later without touching the pipeline or the API.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import ProcessResult

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
RECEIPTS_DIR = DATA_DIR / "receipts"
DB_PATH = DATA_DIR / "expenses.db"


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY, created_at TEXT, vendor TEXT, date TEXT, total REAL, currency TEXT,
            category TEXT, state TEXT, iterations INTEGER, self_correct INTEGER, model TEXT,
            result_json TEXT, image_path TEXT, pdf_path TEXT)"""
    )
    cols = {r["name"] for r in c.execute("PRAGMA table_info(receipts)")}
    for col in ("submitted_by", "department", "emailed_to"):
        if col not in cols:
            c.execute(f"ALTER TABLE receipts ADD COLUMN {col} TEXT")
    c.execute(
        """CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT, department TEXT,
            manager_email TEXT)"""
    )
    c.execute("CREATE TABLE IF NOT EXISTS org (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, name TEXT, picture TEXT, domain TEXT, created_at TEXT, last_login TEXT)"""
    )
    c.execute("CREATE TABLE IF NOT EXISTS login_codes (email TEXT PRIMARY KEY, code_hash TEXT, expires_at REAL, attempts INTEGER DEFAULT 0)")
    return c


# ------------------------------------------------------------------ users / login codes
def upsert_user(email: str, name: str, picture: str, domain: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "INSERT INTO users (email, name, picture, domain, created_at, last_login) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), users.name), "
            "picture=COALESCE(NULLIF(excluded.picture,''), users.picture), last_login=excluded.last_login",
            (email, name, picture, domain, now, now),
        )


def get_user(email: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(r) if r else None


def list_users() -> List[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT email, name, domain, created_at, last_login FROM users ORDER BY last_login DESC")]


def save_login_code(email: str, code_hash: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, attempts) VALUES (?,?,?,0)", (email, code_hash, expires_at))


def get_login_code(email: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM login_codes WHERE email=?", (email,)).fetchone()
    return dict(r) if r else None


def bump_login_attempts(email: str) -> None:
    with _conn() as c:
        c.execute("UPDATE login_codes SET attempts=attempts+1 WHERE email=?", (email,))


def delete_login_code(email: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM login_codes WHERE email=?", (email,))


def save(res: ProcessResult, original_jpeg: bytes, pdf: bytes) -> None:
    img_path = RECEIPTS_DIR / f"{res.id}.jpg"
    pdf_path = RECEIPTS_DIR / f"{res.id}.pdf"
    img_path.write_bytes(original_jpeg)
    pdf_path.write_bytes(pdf)
    res.pdf_path = str(pdf_path)
    slim = res.model_copy(update={"original_image_b64": "", "processed_image_b64": ""})
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO receipts (id, created_at, vendor, date, total, currency, category, state, iterations, "
            "self_correct, model, result_json, image_path, pdf_path, submitted_by, department) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                res.id, datetime.now().isoformat(timespec="seconds"), res.profile.vendor_name, res.profile.date,
                res.profile.total, res.profile.currency, res.category, res.state.value, res.iterations,
                int(res.self_correction_enabled), res.model, slim.model_dump_json(), str(img_path), str(pdf_path),
                res.submitted_by, res.department,
            ),
        )


def list_receipts(limit: int = 200) -> List[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, vendor, date, total, currency, category, state, iterations, self_correct, model, "
            "submitted_by, department, emailed_to FROM receipts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_emailed(rid: str, to: str) -> None:
    with _conn() as c:
        c.execute("UPDATE receipts SET emailed_to=? WHERE id=?", (to, rid))


# ------------------------------------------------------------------ organisation
def get_org() -> dict:
    with _conn() as c:
        kv = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM org")}
        people = [dict(r) for r in c.execute("SELECT * FROM people ORDER BY department, name")]
    return {"org_name": kv.get("org_name", ""), "finance_email": kv.get("finance_email", ""), "people": people}


def set_org(org_name: str, finance_email: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO org VALUES ('org_name', ?)", (org_name,))
        c.execute("INSERT OR REPLACE INTO org VALUES ('finance_email', ?)", (finance_email,))


def add_person(name: str, email: str, department: str, manager_email: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO people (name, email, department, manager_email) VALUES (?,?,?,?)",
                        (name, email, department, manager_email))
        return int(cur.lastrowid)


def delete_person(pid: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM people WHERE id=?", (pid,))


def get_person(name: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM people WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def get(rid: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    result = json.loads(d.pop("result_json"))
    img = Path(d["image_path"])
    if img.exists():
        result["original_image_b64"] = base64.standard_b64encode(img.read_bytes()).decode("ascii")
    d["result"] = result
    return d


def pdf_bytes(rid: str) -> Optional[bytes]:
    p = RECEIPTS_DIR / f"{rid}.pdf"
    return p.read_bytes() if p.exists() else None


def image_bytes(rid: str) -> Optional[bytes]:
    p = RECEIPTS_DIR / f"{rid}.jpg"
    return p.read_bytes() if p.exists() else None


def delete(rid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM receipts WHERE id=?", (rid,))
    for ext in (".jpg", ".pdf"):
        p = RECEIPTS_DIR / f"{rid}{ext}"
        if p.exists():
            p.unlink()


def find_similar(vendor: Optional[str], date: Optional[str], total: Optional[float]) -> Optional[dict]:
    """Duplicate-submission check: same total and date, and vendor name overlapping."""
    if total is None or not date:
        return None
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, vendor FROM receipts WHERE date=? AND ABS(total-?)<0.01 ORDER BY created_at DESC", (date, total)
        ).fetchall()
    v = (vendor or "").lower().split()
    for r in rows:
        rv = (r["vendor"] or "").lower()
        if not v or any(tok in rv for tok in v if len(tok) > 2):
            return dict(r)
    return None


POLICY_PATH = DATA_DIR / "policy.txt"


def get_policy(default: str) -> str:
    return POLICY_PATH.read_text(encoding="utf-8") if POLICY_PATH.exists() else default


def set_policy(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(text, encoding="utf-8")


def stats() -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN state='VERIFIED' THEN 1 ELSE 0 END) verified, "
            "SUM(CASE WHEN iterations>0 THEN 1 ELSE 0 END) corrected FROM receipts"
        ).fetchone()
        by_cur = c.execute(
            "SELECT COALESCE(currency,'INR') currency, COUNT(*) n, COALESCE(SUM(total),0) total "
            "FROM receipts GROUP BY COALESCE(currency,'INR') ORDER BY total DESC"
        ).fetchall()
    d = dict(row)
    d["by_currency"] = [dict(r) for r in by_cur]   # never add different currencies together
    return d
