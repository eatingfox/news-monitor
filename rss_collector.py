"""
News Monitor — RSS 수집기.

config 의 rss.feeds 에 등록된 RSS 2.0 / Atom 피드를 주기적으로 읽어, 새 항목만
data/news/YYYY-MM-DD.md 에 기록한다. digest.py 는 이 파일을 읽어 브리핑을 만든다.

- 공개적으로 제공되는 RSS 만 대상으로 한다(기본값: 연준·SEC·한국은행 공식 피드).
- 기사 원문 페이지는 긁지 않는다. 피드에 포함된 제목·요약·링크만 저장한다.
- 중복 제거는 seen.db(SQLite). 실행 상태는 runs 테이블에 남긴다(digest.py 가 참조).

수동 실행:  python rss_collector.py --once          # 1회 수집
           python rss_collector.py                 # poll_interval_seconds 마다 반복
           python rss_collector.py --once --no-send  # 텔레그램 전송 없이
"""
import argparse
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from secrets_loader import load_config

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "news"
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "seen.db"
ATOM = "{http://www.w3.org/2005/Atom}"
DEFAULT_UA = "news-monitor-rss/1.0"

log = logging.getLogger("rss")


# --------------------------------------------------------------------------- #
def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_DIR / "collector.log", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS seen_news (
        id TEXT PRIMARY KEY, url TEXT, title TEXT, source TEXT,
        file_date TEXT, first_seen TEXT, sent_telegram INTEGER DEFAULT 0)""")
    con.execute("CREATE TABLE IF NOT EXISTS runs (ts TEXT, status TEXT, detail TEXT)")
    con.commit()
    return con


def record_run(con, status, detail=""):
    con.execute("INSERT INTO runs (ts, status, detail) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), status, detail[:500]))
    con.commit()


# --------------------------------------------------------------------------- #
def _text(el, *tags):
    for t in tags:
        v = el.findtext(t)
        if v and v.strip():
            return v.strip()
    return ""


def _clean(s, limit):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)                      # RSS 2.0 (RFC 822)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom (ISO 8601)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(raw, feed_name, summary_chars):
    """RSS 2.0 / Atom 을 공통 형식(dict 리스트)으로."""
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):                          # RSS 2.0
        items.append({
            "title": _clean(_text(it, "title"), 300),
            "url": _text(it, "link", "guid"),
            "summary": _clean(_text(it, "description"), summary_chars),
            "published": _parse_date(_text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
            "source": feed_name,
        })
    for it in root.iter(f"{ATOM}entry"):                  # Atom
        link = it.find(f"{ATOM}link")
        items.append({
            "title": _clean(_text(it, f"{ATOM}title"), 300),
            "url": (link.get("href") if link is not None else "") or _text(it, f"{ATOM}id"),
            "summary": _clean(_text(it, f"{ATOM}summary", f"{ATOM}content"), summary_chars),
            "published": _parse_date(_text(it, f"{ATOM}published", f"{ATOM}updated")),
            "source": feed_name,
        })
    return [i for i in items if i["title"]]


def fetch(url, user_agent, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def item_id(it):
    key = it["url"] or f'{it["source"]}|{it["title"]}|{it["published"]}'
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- #
def daily_file(kst_dt):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fpath = DATA_DIR / f"{kst_dt:%Y-%m-%d}.md"
    if not fpath.exists():
        fpath.write_text(f"# 뉴스 — {kst_dt:%Y-%m-%d} (KST)\n\n", encoding="utf-8")
    return fpath


def write_item(it, kst):
    """digest.parse_articles 가 읽는 블록 형식과 동일하게 기록한다."""
    block = [f"## {kst:%H:%M} · {it['title']}", "",
             f"- 🕒 {kst:%Y-%m-%d %H:%M} KST  ·  📰 {it['source']}"]
    if it["url"]:
        block.append(f"- 🔗 {it['url']}")
    block += ["", it["summary"] or "_(요약 없음)_", "", "---", ""]
    with daily_file(kst).open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")


# --------------------------------------------------------------------------- #
def telegram_send(cfg, text):
    tg = cfg.get("telegram") or {}
    token, chat_id = (tg.get("bot_token") or "").strip(), str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception as e:
        log.warning("telegram 전송 실패: %s", e)
        return False


def should_notify(cfg, it):
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled"):
        return False
    kws = (tg.get("filter") or {}).get("keywords") or []
    if not kws:
        return True
    hay = f"{it['title']} {it['summary']}".lower()
    return any(k.lower() in hay for k in kws)


# --------------------------------------------------------------------------- #
def collect_once(cfg, con, send=True):
    rss = cfg.get("rss") or {}
    feeds = rss.get("feeds") or []
    tz = ZoneInfo(cfg.get("file_date_timezone", "Asia/Seoul"))
    max_age = timedelta(hours=rss.get("max_age_hours", 48))
    summary_chars = rss.get("summary_chars", 600)
    timeout = rss.get("timeout_seconds", 20)
    ua_default = rss.get("user_agent") or DEFAULT_UA
    sec_ua = (cfg.get("btc_treasury") or {}).get("sec_user_agent", "")
    now = datetime.now(timezone.utc)

    new, errors = 0, []
    for feed in feeds:
        name, url = feed.get("name", "feed"), feed.get("url", "")
        # SEC 공정접근 정책: 연락처가 담긴 User-Agent 필요
        ua = sec_ua if ("sec.gov" in url and sec_ua) else ua_default
        try:
            items = parse_feed(fetch(url, ua, timeout), name, summary_chars)
        except Exception as e:
            errors.append(f"{name}: {e}")
            log.warning("피드 실패 %s: %s", name, e)
            continue
        for it in sorted(items, key=lambda x: x["published"] or now):
            pub = it["published"] or now
            if now - pub > max_age:
                continue
            iid = item_id(it)
            if con.execute("SELECT 1 FROM seen_news WHERE id=?", (iid,)).fetchone():
                continue
            kst = pub.astimezone(tz)
            write_item(it, kst)
            sent = 0
            if send and should_notify(cfg, it):
                msg = f"📰 <b>{html.escape(it['title'])}</b>\n{html.escape(it['source'])} · {kst:%m-%d %H:%M}\n{it['url']}"
                sent = int(telegram_send(cfg, msg))
            con.execute("INSERT INTO seen_news VALUES (?,?,?,?,?,?,?)",
                        (iid, it["url"], it["title"], name, f"{kst:%Y-%m-%d}",
                         now.isoformat(), sent))
            con.commit()
            new += 1
            log.info("NEW %s | %s | %s", name, f"{kst:%m-%d %H:%M}", it["title"][:70])
        time.sleep(rss.get("delay_between_feeds_seconds", 1.0))

    status = "ok" if len(errors) < max(1, len(feeds)) else "error"
    record_run(con, status, f"new={new} errors={len(errors)} " + "; ".join(errors)[:300])
    return new, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회만 수집")
    ap.add_argument("--no-send", action="store_true", help="텔레그램 전송 안 함")
    args = ap.parse_args()
    setup_logging()
    con = init_db()
    while True:
        cfg = load_config()
        new, errors = collect_once(cfg, con, send=not args.no_send)
        log.info("수집 완료: 신규 %d건, 실패 피드 %d개", new, len(errors))
        if args.once:
            break
        time.sleep(max(60, int(cfg.get("poll_interval_seconds", 900))))


if __name__ == "__main__":
    main()
