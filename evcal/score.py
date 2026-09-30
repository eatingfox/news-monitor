"""중요도 = 시총계수 × 불확실성 × 임박성. 관심종목은 점수와 무관하게 상단 고정(렌더에서 처리)."""
from datetime import date, datetime

from .tz import KST


def tier_for(ticker: str, cfg) -> str:
    tiers = (cfg.get("calendar") or {}).get("ticker_tier") or {}
    t = (ticker or "").upper()
    for name, lst in tiers.items():
        if t in [x.upper() for x in lst]:
            return name
    return "large"


def imminence(d_days: int) -> float:
    if d_days <= 0:
        return 1.0
    if d_days == 1:
        return 0.8
    if d_days <= 3:
        return 0.5
    return 0.2


def score(ev: dict, cfg, today: date) -> float:
    factors = (cfg.get("calendar") or {}).get("mcap_factor") or {}
    tier = ev.get("tier")
    if not tier:
        tks = ev.get("tickers") or []
        tier = tier_for(tks[0], cfg) if tks else "large"
        ev["tier"] = tier
    f = float(factors.get(tier, 0.7))
    unc = float(ev.get("uncertainty") or 0.5)
    try:
        d = datetime.fromisoformat(ev["when_kst"]).astimezone(KST).date()
        dd = (d - today).days
    except Exception:
        dd = 7
    return round(f * unc * imminence(dd), 3)
