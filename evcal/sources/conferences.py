"""연간 컨퍼런스 마스터(수동 관리, conferences.json). 월 단위 → 일자 미확정으로 표기."""
import json
from pathlib import Path
from datetime import date

from ..tz import KST, ET, at
from ..db import make_id

HERE = Path(__file__).resolve().parent


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    try:
        items = json.loads((HERE / "conferences.json").read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[conf] 마스터 로드 실패: {e}")
        return []
    out = []
    for it in items:
        d = None
        confirmed = False
        if it.get("date"):
            try:
                d = date.fromisoformat(it["date"])
                confirmed = True
            except ValueError:
                pass
        elif it.get("month"):
            y, m = it["month"].split("-")
            d = date(int(y), int(m), 1)  # 월 초로 배치, 렌더에서 '일자 미확정' 처리
        if not d or not (start <= d <= end or (not confirmed and d.year == start.year and d.month in (start.month, end.month))):
            continue
        w = at(d, 9, 0, KST)
        out.append({
            "id": make_id("conf", it["name"] + (it.get("date") or it.get("month", ""))),
            "name": it["name"], "category": "conf", "tickers": it.get("tickers", []),
            "when_et": w.astimezone(ET).isoformat(),
            "when_kst": w.isoformat(), "all_day": True, "tier": "sector",
            "uncertainty": it.get("uncertainty", 0.4), "impact": it.get("sector", ""),
            "date_confirmed": confirmed, "src": "conf", "is_watchlist": False,
            "note": None if confirmed else "월 단위·정확 일자 확인 필요",
        })
    return out
