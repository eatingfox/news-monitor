"""events.db (SQLite) — 스키마, upsert, 조회."""
import json
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "events.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  name TEXT, category TEXT, tickers TEXT,
  when_et TEXT, when_kst TEXT, all_day INTEGER DEFAULT 0,
  us_session TEXT, kr_session TEXT,
  consensus TEXT, impact TEXT,
  tier TEXT, uncertainty REAL, importance REAL,
  is_watchlist INTEGER DEFAULT 0, date_confirmed INTEGER DEFAULT 1,
  url TEXT, src TEXT, note TEXT,
  first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'upcoming'
);
CREATE INDEX IF NOT EXISTS idx_events_kst ON events(when_kst);
CREATE TABLE IF NOT EXISTS calendar_gaps (
  date TEXT, ticker TEXT, move_pct REAL, had_event INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS edgar_parsed (   -- 8-K 본문 파싱 캐시(접수번호별, 재요청 방지)
  acc TEXT PRIMARY KEY, ticker TEXT, filed TEXT, parsed_at TEXT, events TEXT
);
"""

COLS = ("id", "name", "category", "tickers", "when_et", "when_kst", "all_day",
        "us_session", "kr_session", "consensus", "impact", "tier", "uncertainty",
        "importance", "is_watchlist", "date_confirmed", "url", "src", "note",
        "first_seen", "last_seen", "status")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def make_id(src: str, key: str) -> str:
    return f"{src}:{hashlib.md5(key.encode('utf-8')).hexdigest()[:12]}"


def upsert(con: sqlite3.Connection, ev: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = con.execute("SELECT first_seen FROM events WHERE id=?", (ev["id"],)).fetchone()
    first = row[0] if row else now
    con.execute(
        f"INSERT OR REPLACE INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
        (
            ev["id"], ev.get("name", ""), ev.get("category", ""),
            json.dumps(ev.get("tickers", []), ensure_ascii=False),
            ev.get("when_et"), ev.get("when_kst"), 1 if ev.get("all_day") else 0,
            ev.get("us_session"), ev.get("kr_session"),
            ev.get("consensus"), ev.get("impact"),
            ev.get("tier"), ev.get("uncertainty"), ev.get("importance"),
            1 if ev.get("is_watchlist") else 0,
            1 if ev.get("date_confirmed", True) else 0,
            ev.get("url"), ev.get("src"), ev.get("note"),
            first, now, ev.get("status", "upcoming"),
        ),
    )


def events_between(con, start_kst_iso: str, end_kst_iso: str) -> list[dict]:
    cur = con.execute(
        "SELECT * FROM events WHERE when_kst>=? AND when_kst<? ORDER BY when_kst",
        (start_kst_iso, end_kst_iso),
    )
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        try:
            d["tickers"] = json.loads(d.get("tickers") or "[]")
        except Exception:
            d["tickers"] = []
        out.append(d)
    return out


def mark_past_done(con, now_kst_iso: str) -> None:
    con.execute("UPDATE events SET status='done' WHERE when_kst<? AND status='upcoming'",
                (now_kst_iso,))
