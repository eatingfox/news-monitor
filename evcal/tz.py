"""시간대·세션·날짜 규칙 헬퍼. ET/KST 변환은 DST를 zoneinfo 로 처리."""
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
JST = ZoneInfo("Asia/Tokyo")
CET = ZoneInfo("Europe/Berlin")


def at(d: date, hh: int, mm: int = 0, tz=ET) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=tz)


def to_kst(dt: datetime) -> datetime:
    return dt.astimezone(KST)


def to_et(dt: datetime) -> datetime:
    return dt.astimezone(ET)


def now_kst() -> datetime:
    return datetime.now(KST)


def us_session(dt: datetime) -> str:
    t = to_et(dt).time()
    if t < time(9, 30):
        return "장전"
    if t < time(16, 0):
        return "장중"
    return "장후"


def kr_session(dt: datetime) -> str:
    t = to_kst(dt).time()
    if t < time(9, 0):
        return "장전"
    if t < time(15, 30):
        return "장중"
    return "장후"


def nth_weekday(year: int, month: int, weekday: int, n: int):
    """month 의 n번째 weekday(월=0). 없으면 None."""
    d = date(year, month, 1)
    d = d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    return d if d.month == month else None


def nth_business_day(year: int, month: int, n: int, holidays=()):
    d = date(year, month, 1)
    c = 0
    while True:
        if d.weekday() < 5 and d not in holidays:
            c += 1
            if c == n:
                return d
        d += timedelta(days=1)


def month_range(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1
