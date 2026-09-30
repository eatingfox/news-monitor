"""미국·한국 증시 휴장일 마스터 (2026). 공개 정보 기반 — 연초에 갱신·확인 권장.

미국 휴장 = '미국 리드 없는 날' 배너에 사용.
"""
from datetime import date

from ..tz import ET, KST, at
from ..db import make_id

US_2026 = {
    date(2026, 1, 1): "신정", date(2026, 1, 19): "마틴루터킹데이", date(2026, 2, 16): "대통령의 날",
    date(2026, 4, 3): "성금요일", date(2026, 5, 25): "메모리얼데이", date(2026, 6, 19): "준틴스",
    date(2026, 7, 3): "독립기념일(대체)", date(2026, 9, 7): "노동절",
    date(2026, 11, 26): "추수감사절", date(2026, 12, 25): "크리스마스",
}
KR_2026 = {
    date(2026, 1, 1): "신정", date(2026, 2, 16): "설연휴", date(2026, 2, 17): "설날",
    date(2026, 2, 18): "설연휴", date(2026, 3, 2): "삼일절(대체)", date(2026, 5, 1): "근로자의날",
    date(2026, 5, 5): "어린이날", date(2026, 5, 25): "부처님오신날(대체)", date(2026, 6, 3): "지방선거",
    date(2026, 8, 17): "광복절(대체)", date(2026, 9, 24): "추석연휴", date(2026, 9, 25): "추석",
    date(2026, 9, 26): "추석연휴", date(2026, 10, 5): "개천절(대체)", date(2026, 10, 9): "한글날",
    date(2026, 12, 25): "크리스마스", date(2026, 12, 31): "연말휴장",
}


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    out = []
    for d, nm in US_2026.items():
        if start <= d <= end:
            w = at(d, 9, 30, ET)
            out.append({"id": make_id("hol", f"US-{d}"), "name": f"🇺🇸 미국 휴장 — {nm} (미국 리드 없는 날)",
                        "category": "holiday", "tickers": [], "when_et": w.isoformat(),
                        "when_kst": w.astimezone(KST).isoformat(), "all_day": True,
                        "tier": "macro", "uncertainty": 0.1, "impact": "미국 부재·아시아 자체 흐름",
                        "date_confirmed": True, "src": "hol", "is_watchlist": False,
                        "note": "market_us_closed"})
    for d, nm in KR_2026.items():
        if start <= d <= end:
            w = at(d, 9, 0, KST)
            out.append({"id": make_id("hol", f"KR-{d}"), "name": f"🇰🇷 한국 휴장 — {nm}",
                        "category": "holiday", "tickers": [], "when_et": w.astimezone(ET).isoformat(),
                        "when_kst": w.isoformat(), "all_day": True,
                        "tier": "macro", "uncertainty": 0.1, "impact": "국내 휴장",
                        "date_confirmed": True, "src": "hol", "is_watchlist": False,
                        "note": "market_kr_closed"})
    return out
