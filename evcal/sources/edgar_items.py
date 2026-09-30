"""SEC 8-K 본문 파싱(Phase 2) — 공시 문장에서 '예고된 미래 이벤트'를 뽑는다.

대상 문장 예:
  "will host an Investor Day on Thursday, October 8, 2026"
  "will participate in the Morgan Stanley Global Healthcare Conference on September 15, 2026"
  "will report third quarter results on October 28, 2026 ... at 4:30 p.m. Eastern Time"
원칙: 날짜는 공시 원문에서 정규식으로만 추출(추정 금지). 날짜가 없는 문장은 버린다.
"""
import re
import html as _html
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from ..tz import ET, KST
from ..db import make_id

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
# "September 14, 2026" / "Sept. 14-16, 2026" / "September 14 through 16, 2026" / "September 14" (연도 생략)
DATE_RE = re.compile(
    _MON + r"\s+(\d{1,2})\s*(?:st|nd|rd|th)?"
    r"(?:\s*(?:-|–|—|to|through|and)\s*(\d{1,2})\s*(?:st|nd|rd|th)?)?"
    r"(?:\s*,?\s*(20\d{2}))?",
    re.I,
)
TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\.?\s*"
    r"(?:\(?\s*(Eastern|ET\b|EDT|EST|Pacific|PT\b|PDT|PST|Central|CT\b|CDT|CST|Mountain|MT\b|MDT|MST))?",
    re.I,
)
TZ_MAP = {
    "eastern": "America/New_York", "et": "America/New_York", "edt": "America/New_York", "est": "America/New_York",
    "pacific": "America/Los_Angeles", "pt": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "central": "America/Chicago", "ct": "America/Chicago", "cdt": "America/Chicago", "cst": "America/Chicago",
    "mountain": "America/Denver", "mt": "America/Denver", "mdt": "America/Denver", "mst": "America/Denver",
}

# 분류 규칙 (kind, 필수 정규식, 이름, 불확실성, 영향)
KINDS = [
    ("invday", re.compile(r"\b(investor|analyst|capital markets)\s+(day|event|meeting|conference)\b", re.I),
     "인베스터데이/애널리스트데이 (8-K 예고)", 0.85, "가이던스·장기 목표 공개"),
    ("earnings", re.compile(
        r"\b(report|announce|release|discuss|host|hold|publish)\w*\b[^.]{0,120}?\b(results|earnings)\b"
        r"|\b(earnings|results)\s+(conference\s+)?call\b", re.I),
     "실적 발표 (8-K 예고)", 0.7, "실적·가이던스"),
    ("product", re.compile(r"\b(launch event|product event|unveil\w*|reveal event|demo day|keynote)\b", re.I),
     "제품/런칭 이벤트 (8-K 예고)", 0.75, "신제품·수요 읽기"),
    ("conf", re.compile(r"\b(participat\w+|present\w*|fireside|attend\w*|speak\w*|appear\w*)\b[^.]{0,160}?"
                        r"\b(conference|summit|forum|symposium)\b", re.I),
     "컨퍼런스 참석", 0.55, "경영진 즉석 코멘트·가이던스 톤"),
    ("agm", re.compile(r"\bannual meeting of (stock|share)holders\b|\bannual (stockholders'?|shareholders'?) meeting\b", re.I),
     "주주총회", 0.3, "의결·주주환원"),
]
CONF_NAME_RE = re.compile(
    r"\b(?:the\s+)?((?:\d{4}\s+)?(?:[A-Z][\w&.'’-]*|\d+(?:st|nd|rd|th))(?:\s+(?:[A-Z][\w&.'’-]*|&|\+|and|of|de|\d+(?:st|nd|rd|th)))*"
    r"\s+(?:Conference|Summit|Forum|Symposium|Investor Day|Analyst Day))\b")
BOILERPLATE = re.compile(r"forward-looking|safe harbor|securities exchange act|incorporated by reference", re.I)
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
HTML_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    raw = TAG_RE.sub(" ", raw)
    raw = re.sub(r"</(p|div|tr|li|br|h\d)>", "\n", raw, flags=re.I)
    txt = HTML_RE.sub(" ", raw)
    txt = _html.unescape(txt).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", txt)


def split_sentences(txt: str):
    txt = re.sub(r"\s*\n\s*", " ", txt)
    # 약어 뒤 마침표는 문장 끝이 아님
    txt = re.sub(r"\b(Sept|Inc|Corp|Ltd|Co|Mr|Ms|Dr|p\.m|a\.m|U\.S|No|vs|approx)\.", lambda m: m.group(0).replace(".", "§"), txt, flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", txt)
    return [p.replace("§", ".").strip() for p in parts if p.strip()]


def _year_for(month: int, day: int, year, filed: date) -> date | None:
    try:
        if year:
            return date(int(year), month, day)
        d = date(filed.year, month, day)
        if d < filed:  # 공시일보다 과거면 다음 해로
            d = date(filed.year + 1, month, day)
        return d
    except ValueError:
        return None


PERIOD_RE = re.compile(r"(end(?:ed|ing)|as of|beginning|since|from|dated|period of|between)\s*$", re.I)
FUTURE_RE = re.compile(r"\b(will|plans? to|is scheduled|are scheduled|scheduled for|expects? to|intends? to|"
                       r"look forward to|to be held|invites?|announce[sd]? (?:that|the date)|has scheduled)\b", re.I)


def dates_in(sent: str, filed: date) -> list[date]:
    out = []
    for m in DATE_RE.finditer(sent):
        mon = MONTHS.get(m.group(1).lower().rstrip("."))
        if not mon:
            continue
        if PERIOD_RE.search(sent[max(0, m.start() - 24):m.start()]):
            continue  # "quarter ending September 30, 2026" 같은 회계기간 표기는 이벤트 날짜가 아님
        d1 = _year_for(mon, int(m.group(2)), m.group(4), filed)
        if d1:
            out.append(d1)
        if m.group(3):
            d2 = _year_for(mon, int(m.group(3)), m.group(4), filed)
            if d2 and d1 and d2 > d1 and (d2 - d1).days <= 7:
                pass  # 기간 종료일은 name에만 반영(이벤트는 시작일 1건)
    # 중복 제거·순서 유지
    seen, res = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


def date_range_label(sent: str) -> str:
    m = DATE_RE.search(sent)
    if m and m.group(3):
        return f"{int(m.group(2))}~{int(m.group(3))}일"
    return ""


def time_in(sent: str):
    m = TIME_RE.search(sent)
    if not m:
        return None, None
    hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    mm = int(m.group(2) or 0)
    tz = TZ_MAP.get((m.group(4) or "eastern").lower().rstrip("."), "America/New_York")
    return dtime(hh, mm), ZoneInfo(tz)


def classify(sent: str):
    if BOILERPLATE.search(sent) or not FUTURE_RE.search(sent):
        return None
    for kind, rx, label, unc, impact in KINDS:
        if rx.search(sent):
            return kind, label, unc, impact
    return None


def extract(ticker: str, text: str, filed: date, today: date, end: date, url: str, acc: str) -> list[dict]:
    """공시 본문 → 미래 이벤트 목록. today <= 이벤트일 <= end 인 것만."""
    out, seen = [], set()
    sents = split_sentences(text)
    for i, s in enumerate(sents):
        c = classify(s)
        if not c:
            continue
        kind, label, unc, impact = c
        ds = dates_in(s, filed)
        if not ds and i + 1 < len(sents):  # 날짜가 다음 문장에 있는 경우("The event will be held on …")
            nxt = sents[i + 1]
            if re.search(r"\b(will be held|is scheduled|will take place|scheduled for)\b", nxt, re.I):
                ds = dates_in(nxt, filed)
                s = s + " " + nxt
        ds = [d for d in ds if d > filed and today <= d <= end]
        if not ds:
            continue
        name = f"{ticker} {label}"
        if kind == "conf":
            cm = CONF_NAME_RE.search(s)
            cname = cm.group(1).strip() if cm else "컨퍼런스"
            name = f"{ticker} 컨퍼런스 참석: {cname}"
        rng = date_range_label(s)
        if rng:
            name += f" ({rng})"
        t, tz = time_in(s)
        for d in ds[:1] if kind != "conf" else ds[:1]:
            key = (ticker, kind, d, name)
            if key in seen:
                continue
            seen.add(key)
            all_day = t is None
            w = datetime.combine(d, t or dtime(9, 0), tzinfo=tz or ET)
            snippet = re.sub(r"\s+", " ", s)[:220]
            out.append({
                "id": make_id("edgar8k", f"{ticker}|{kind}|{d.isoformat()}|{name}"),
                "name": name, "category": "earnings" if kind == "earnings" else ("conf" if kind == "conf" else "corp"),
                "tickers": [ticker], "when_et": w.astimezone(ET).isoformat(),
                "when_kst": w.astimezone(KST).isoformat(), "all_day": all_day,
                "tier": None, "uncertainty": unc, "impact": impact,
                "date_confirmed": True, "url": url, "src": "edgar8k", "is_watchlist": True,
                "note": f"8-K {acc} 원문: “{snippet}”",
            })
    return out
