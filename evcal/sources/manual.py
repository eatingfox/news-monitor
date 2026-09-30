"""수동 확정 이벤트(manual.json) — IR데이·실적일·제품행사 등 자동 소스가 아직 못 잡는 것을 사람이 등록.
필드: name, date(YYYY-MM-DD), time(HH:MM, 선택), tz(IANA, 기본 America/New_York),
      tickers, category(corp|earnings|conf|policy), uncertainty, impact, url, confirmed(기본 true)
"""
import json
from pathlib import Path
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from ..tz import ET, KST
from ..db import make_id

HERE = Path(__file__).resolve().parent


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    try:
        items = json.loads((HERE / "manual.json").read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[manual] 로드 실패: {e}")
        return []
    out = []
    for it in items:
        try:
            d = date.fromisoformat(it["date"])
        except Exception:
            continue
        if not (start <= d <= end):
            continue
        tz = ZoneInfo(it.get("tz") or "America/New_York")
        all_day = not it.get("time")
        if all_day:
            w = datetime.combine(d, dtime(9, 0), tzinfo=tz)
        else:
            hh, mm = it["time"].split(":")
            w = datetime.combine(d, dtime(int(hh), int(mm)), tzinfo=tz)
        out.append({
            "id": make_id("manual", it["name"] + it["date"]),
            "name": it["name"], "category": it.get("category", "corp"),
            "tickers": it.get("tickers", []),
            "when_et": w.astimezone(ET).isoformat(), "when_kst": w.astimezone(KST).isoformat(),
            "all_day": all_day, "tier": None, "uncertainty": it.get("uncertainty", 0.7),
            "impact": it.get("impact", ""), "date_confirmed": it.get("confirmed", True),
            "url": it.get("url"), "src": "manual", "is_watchlist": bool(it.get("tickers")),
            "note": "수동 등록" + ("" if it.get("confirmed", True) else "(일자 미검증·확인필요)"),
        })
    log(f"[manual] {len(out)}건")
    return out
