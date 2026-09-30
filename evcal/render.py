"""디제스트 상단 '■ 오늘·내일 이벤트 (KST)' 섹션을 코드로 렌더.
날짜·시각은 events.db 확정값만 사용(LLM 환각 방지)."""
from datetime import datetime, timedelta

from .db import events_between
from .tz import KST, now_kst


def _fmt_time(ev):
    if ev.get("all_day"):
        return "종일 "
    try:
        return datetime.fromisoformat(ev["when_kst"]).astimezone(KST).strftime("%H:%M")
    except Exception:
        return "--:--"


def _line(ev, show_date=False):
    t = _fmt_time(ev)
    if show_date:
        try:
            t = datetime.fromisoformat(ev["when_kst"]).astimezone(KST).strftime("%m/%d ") + t
        except Exception:
            pass
    tags = []
    if ev.get("category") != "holiday":
        us, kr = ev.get("us_session"), ev.get("kr_session")
        if us and kr:
            tags.append(f"美{us}·韓{kr}")
    if not ev.get("date_confirmed"):
        tags.append("예상일")
    star = "★" if ev.get("is_watchlist") else ""
    cons = f" | 컨센 {ev['consensus']}" if ev.get("consensus") else ""
    imp = f" | 영향: {ev['impact']}" if ev.get("impact") else ""
    tg = f" [{' · '.join(tags)}]" if tags else ""
    return f"{t} KST | {star}{ev['name']}{cons}{tg}{imp}"


def render_calendar_section(con, cfg, now=None):
    """returns (section_text_for_telegram, plain_context_for_llm)"""
    now = now or now_kst()
    today = now.date()
    d0 = datetime.combine(today, datetime.min.time(), tzinfo=KST)
    d2 = d0 + timedelta(days=2)
    d4 = d0 + timedelta(days=4)

    evs = events_between(con, d0.isoformat(), d2.isoformat())
    soon = events_between(con, d2.isoformat(), d4.isoformat())

    # 휴장 배너
    banners = []
    for e in evs:
        if e.get("category") == "holiday" and "market_us_closed" in (e.get("note") or ""):
            banners.append(f"⚠️ {e['name']}")
        elif e.get("category") == "holiday":
            banners.append(f"⚠️ {e['name']}")
    core = [e for e in evs if e.get("category") != "holiday"]
    # 정렬: 관심종목 상단 → 시각순
    core.sort(key=lambda e: (0 if e.get("is_watchlist") else 1, e.get("when_kst") or ""))

    # 월 단위(일자 미확정) 컨퍼런스: 오늘/내일 목록에서 빼고, 이번 달 전체에서 모아 별도 줄로
    core = [e for e in core if not (e.get("src") == "conf" and not e.get("date_confirmed"))]
    m_start = d0.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1)
    month_conf = [e for e in events_between(con, m_start.isoformat(), m_end.isoformat())
                  if e.get("src") == "conf" and not e.get("date_confirmed")]

    lines = ["■ 오늘·내일 이벤트 (KST)"]
    lines += banners
    if not core:
        lines.append("(등록된 확정 이벤트 없음)")
    else:
        today_lines, tmr_lines = [], []
        for e in core:
            try:
                d = datetime.fromisoformat(e["when_kst"]).astimezone(KST).date()
            except Exception:
                d = today
            (today_lines if d == today else tmr_lines).append(_line(e))
        if today_lines:
            lines.append(f"[오늘 {today:%m/%d}]")
            lines += today_lines[:10]
        if tmr_lines:
            lines.append(f"[내일 {today + timedelta(days=1):%m/%d}]")
            lines += tmr_lines[:8]
    # D-2~3 리마인드 (중요도 상위·관심종목)
    rem = [e for e in soon if e.get("category") != "holiday"
           and (e.get("is_watchlist") or (e.get("importance") or 0) >= 0.45)]
    rem.sort(key=lambda e: (0 if e.get("is_watchlist") else 1, -(e.get("importance") or 0)))
    if rem:
        lines.append("▶ D-2~3: " + " / ".join(_line(e, show_date=True).split(" | 영향")[0] for e in rem[:5]))
    # 주간 전망: 일요일 저녁·월요일 오전 다이제스트에 향후 7일 ★관심종목 이벤트 나열
    if now.weekday() == 6 or (now.weekday() == 0 and now.hour < 14):
        d7 = d0 + timedelta(days=8)
        week = [e for e in events_between(con, d0.isoformat(), d7.isoformat())
                if e.get("category") != "holiday" and e.get("is_watchlist")
                and not (e.get("src") == "edgar" and "공시 제출" in (e.get("name") or ""))]
        week.sort(key=lambda e: e.get("when_kst") or "")
        if week:
            lines.append("▶ 이번 주 ★: " + " / ".join(
                _line(e, show_date=True).split(" | 영향")[0] for e in week[:8]))
    if month_conf:
        lines.append("이달 예정(일자 확인): " + ", ".join(e["name"] for e in month_conf[:6]))

    text = "\n".join(lines)
    ctx = "\n".join(_line(e, show_date=True) for e in (core + rem)) or "(없음)"
    return text, ctx
