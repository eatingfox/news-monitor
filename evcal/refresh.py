"""캘린더 갱신 오케스트레이터. 스케줄: 매일 06:00 / 17:00 (Task Scheduler).

    python -m evcal.refresh            # 갱신
    python -m evcal.refresh --dry-run  # DB 반영 없이 출력
    python -m evcal.refresh --show     # 오늘·내일 섹션 렌더만
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evcal.tz import KST, now_kst, us_session, kr_session  # noqa: E402
from evcal.db import connect, upsert, mark_past_done  # noqa: E402
from evcal.score import score  # noqa: E402
from evcal.render import render_calendar_section  # noqa: E402
from evcal.sources import macro, holidays, conferences, edgar, dart, manual  # noqa: E402

LOG_DIR = ROOT / "logs"


def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_kst():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with (LOG_DIR / "calendar.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    from secrets_loader import load_config as _load
    return _load()


def _fill_sessions(ev):
    try:
        k = datetime.fromisoformat(ev["when_kst"])
        ev["kr_session"] = kr_session(k) if not ev.get("all_day") else None
        ev["us_session"] = us_session(k) if not ev.get("all_day") else None
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    cal = cfg.get("calendar") or {}
    if not cal.get("enabled", True):
        log("calendar.enabled=false → 건너뜀")
        return
    con = connect()
    if args.show:
        text, _ = render_calendar_section(con, cfg)
        print(text)
        return

    today = now_kst().date()
    start = today - timedelta(days=int(cal.get("lookback_days", 3)))
    end = today + timedelta(days=int(cal.get("lookahead_days", 14)))
    log(f"refresh 시작: {start} ~ {end}")

    events = []
    for name, mod in (("macro", macro), ("holidays", holidays), ("conf", conferences),
                      ("manual", manual), ("edgar", edgar), ("dart", dart)):
        try:
            got = mod.collect(cfg, start, end, log)
            log(f"  {name}: {len(got)}건")
            events += got
        except Exception as e:
            log(f"  {name}: 실패 {e}")

    for ev in events:
        _fill_sessions(ev)
        ev["importance"] = score(ev, cfg, today)

    if args.dry_run:
        for ev in sorted(events, key=lambda e: e["when_kst"]):
            print(f"{ev['when_kst'][:16]} {ev['importance']:.2f} {'★' if ev.get('is_watchlist') else ' '} {ev['name']}")
        return

    for ev in events:
        upsert(con, ev)
    mark_past_done(con, now_kst().isoformat())
    con.commit()
    log(f"refresh 완료: {len(events)}건 upsert")
    text, _ = render_calendar_section(con, cfg)
    print("\n" + text)


if __name__ == "__main__":
    main()
