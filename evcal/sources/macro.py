"""매크로 일정 소스.

TOS: 정부·중앙은행 공개 일정(bls.gov 등)은 공개 데이터. BLS 페이지는 UA 있는 단순 GET 1~3회/일.

원칙(추정 금지):
- FOMC 2026 은 공식 확정 일정 → date_confirmed=1
- BOJ/ECB 2026 은 공개 일정이나 여기 값은 검증 전 → date_confirmed=0 ("확인 권장")
- 한은 금통위는 config.calendar.bok_mpc_dates_2026 에 사용자가 채운 날짜만 사용
- NFP/CPI/PPI 는 BLS 공식 일정 페이지를 라이브로 읽어 확정. 실패 시 규칙 기반 '예상일' 표기(date_confirmed=0)
- 그 외 규칙 기반 지표는 '예상일' 로 표기
"""
import re
import urllib.request
from datetime import date, timedelta

from ..tz import ET, JST, CET, KST, at, to_kst, nth_weekday, nth_business_day, month_range
from ..db import make_id

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")

# ---- 확정/공개 일정 마스터 (2026) ------------------------------------------
FOMC_2026 = [(1, 27, 28), (3, 17, 18), (4, 28, 29), (6, 16, 17),
             (7, 28, 29), (9, 15, 16), (10, 27, 28), (12, 8, 9)]
BOJ_2026 = [(1, 22, 23), (3, 18, 19), (4, 27, 28), (6, 15, 16),
            (7, 30, 31), (9, 17, 18), (10, 29, 30), (12, 17, 18)]   # 확인 권장
ECB_2026 = [(2, 5), (3, 19), (4, 30), (6, 11), (7, 23), (9, 10), (10, 29), (12, 17)]  # 확인 권장

BLS_PAGES = {
    "NFP": ("https://www.bls.gov/schedule/news_release/empsit.htm", "美 8월 고용보고서(NFP)"),
    "CPI": ("https://www.bls.gov/schedule/news_release/cpi.htm", "美 CPI"),
    "PPI": ("https://www.bls.gov/schedule/news_release/ppi.htm", "美 PPI"),
}
# BLS 페이지가 403(봇 차단)일 때 쓰는 '확정 알려진 날짜' — 피드 복수 기사·공식 공지로 확인된 것만.
# (규칙 8: 복수 소스가 규칙 추정과 충돌하면 소스 우선) 연/월 단위로 갱신.
BLS_KNOWN = {
    "CPI": {(2026, 9): (date(2026, 9, 11), "피드 4건+ '금요일 발표' 확인(9/8)")},
    "PPI": {(2026, 9): (date(2026, 9, 10), "피드 4건+ '목요일 발표' 확인(9/8)")},
    "NFP": {(2026, 9): (date(2026, 9, 4), "실제 발표 확인(9/4 21:30 KST)")},
}
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def _ev(src, key, name, when, *, category="macro", tier="macro", uncertainty=0.5,
        impact="", consensus=None, confirmed=True, note=None, url=None, tickers=None,
        all_day=False):
    kst = to_kst(when)
    return {
        "id": make_id(src, key), "name": name, "category": category,
        "tickers": tickers or [], "when_et": when.astimezone(ET).isoformat(),
        "when_kst": kst.isoformat(), "all_day": all_day,
        "tier": tier, "uncertainty": uncertainty, "impact": impact,
        "consensus": consensus, "date_confirmed": confirmed, "note": note,
        "url": url, "src": src, "is_watchlist": False,
    }


def fetch_bls_dates(url: str, log=print):
    """BLS 일정 페이지에서 'Month DD, YYYY' 날짜 집합 추출. 실패 시 None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        log(f"[macro] BLS fetch 실패 {url}: {e}")
        return None
    found = set()
    for mon, dd, yy in re.findall(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})",
            html):
        try:
            found.add(date(int(yy), _MONTHS[mon], int(dd)))
        except ValueError:
            pass
    return found or None


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    out = []
    bok_dates = (cfg.get("calendar") or {}).get("bok_mpc_dates_2026") or []

    def in_win(d):
        return start <= d <= end

    # --- FOMC (확정) ---
    for m, d1, d2 in FOMC_2026:
        d = date(2026, m, d2)
        if in_win(d):
            out.append(_ev("fomc", f"{d}", "美 FOMC 금리결정", at(d, 14, 0, ET),
                           uncertainty=1.0, impact="금리·환율·전체", confirmed=True,
                           url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"))
            out.append(_ev("fomc", f"{d}-presser", "美 FOMC 의장 기자회견", at(d, 14, 30, ET),
                           uncertainty=0.9, impact="금리·환율", confirmed=True))
    # --- BOJ (확인 권장) ---
    for m, d1, d2 in BOJ_2026:
        d = date(2026, m, d2)
        if in_win(d):
            out.append(_ev("boj", f"{d}", "日 BOJ 금리결정", at(d, 12, 0, JST),
                           uncertainty=1.0, impact="엔화·캐리·아시아", confirmed=False,
                           note="공개 일정 기준·확정 확인 권장"))
    # --- ECB (확인 권장) ---
    for m, dd in ECB_2026:
        d = date(2026, m, dd)
        if in_win(d):
            out.append(_ev("ecb", f"{d}", "유럽 ECB 금리결정", at(d, 14, 15, CET),
                           uncertainty=0.9, impact="유로·글로벌금리", confirmed=False,
                           note="공개 일정 기준·확정 확인 권장"))
    # --- 한은 금통위 (사용자 입력) ---
    for s in bok_dates:
        try:
            d = date.fromisoformat(s)
        except Exception:
            continue
        if in_win(d):
            out.append(_ev("bok", f"{d}", "韓 금통위 기준금리 결정", at(d, 10, 0, KST),
                           uncertainty=1.0, impact="원화·국내금리·은행", confirmed=True))

    # --- BLS: NFP/CPI/PPI 라이브 확정, 실패 시 예상 ---
    bls_ok = {}
    for key, (url, label) in BLS_PAGES.items():
        dates = fetch_bls_dates(url, log)
        bls_ok[key] = dates
        if dates:
            for d in sorted(dates):
                if in_win(d):
                    nm = {"NFP": "美 고용보고서(NFP·실업률)", "CPI": "美 소비자물가(CPI)",
                          "PPI": "美 생산자물가(PPI)"}[key]
                    out.append(_ev("bls", f"{key}-{d}", nm, at(d, 8, 30, ET),
                                   uncertainty=0.6 if key != "NFP" else 0.7,
                                   impact="금리·환율·전체", confirmed=True, url=url))
    # BLS 실패 시: ① 확정 알려진 날짜(BLS_KNOWN) 우선 → ② 없으면 규칙 기반 '(예상일)'
    _names = {"NFP": ("美 고용보고서(NFP·실업률)", 0.7, "금리·환율·전체"),
              "CPI": ("美 소비자물가(CPI)", 0.6, "금리·환율"),
              "PPI": ("美 생산자물가(PPI)", 0.5, "금리")}
    _rules = {"NFP": lambda y, m: nth_weekday(y, m, 4, 1),   # 첫 금요일
              "CPI": lambda y, m: nth_weekday(y, m, 2, 2),   # 둘째 수요일 근사
              "PPI": lambda y, m: nth_weekday(y, m, 3, 2)}   # 둘째 목요일 근사
    for y, m in month_range(start, end):
        for key in ("NFP", "CPI", "PPI"):
            if bls_ok.get(key):
                continue
            nm, unc, imp = _names[key]
            known = BLS_KNOWN.get(key, {}).get((y, m))
            if known:
                d, why = known
                if in_win(d):
                    out.append(_ev("bls", f"{key}-{d}", nm, at(d, 8, 30, ET), uncertainty=unc,
                                   impact=imp, confirmed=True, note=f"확정(BLS 403 대체): {why}"))
                continue
            d = _rules[key](y, m)
            if d and in_win(d):
                out.append(_ev("rule", f"{key}-{d}", f"{nm} (예상일)", at(d, 8, 30, ET),
                               uncertainty=unc, impact=imp, confirmed=False,
                               note="규칙 기반 예상일·BLS 확인 필요"))

        # --- 기타 규칙 기반(예상일 표기) ---
        nfp = nth_weekday(y, m, 4, 1)
        if nfp:
            adp = nfp - timedelta(days=2)  # 수요일
            if in_win(adp):
                out.append(_ev("rule", f"ADP-{adp}", "美 ADP 민간고용 (예상일)", at(adp, 8, 15, ET),
                               uncertainty=0.5, impact="금리", confirmed=False))
        ism1 = nth_business_day(y, m, 1)
        if in_win(ism1):
            out.append(_ev("rule", f"ISMm-{ism1}", "美 ISM 제조업 PMI (예상일)", at(ism1, 10, 0, ET),
                           uncertainty=0.5, impact="금리·경기", confirmed=False))
        ism3 = nth_business_day(y, m, 3)
        if in_win(ism3):
            out.append(_ev("rule", f"ISMs-{ism3}", "美 ISM 서비스 PMI (예상일)", at(ism3, 10, 0, ET),
                           uncertainty=0.5, impact="금리·경기", confirmed=False))
        jolts = nth_weekday(y, m, 1, 1)
        if jolts and in_win(jolts):
            out.append(_ev("rule", f"JOLTS-{jolts}", "美 JOLTS 구인 (예상일)", at(jolts, 10, 0, ET),
                           uncertainty=0.4, impact="금리", confirmed=False))
        t10 = nth_weekday(y, m, 2, 2)
        if t10 and in_win(t10):
            out.append(_ev("rule", f"UST10-{t10}", "美 10년물 국채 입찰 (예상일)", at(t10, 13, 0, ET),
                           uncertainty=0.5, impact="장기금리", confirmed=False))
        t30 = nth_weekday(y, m, 3, 2)
        if t30 and in_win(t30):
            out.append(_ev("rule", f"UST30-{t30}", "美 30년물 국채 입찰 (예상일)", at(t30, 13, 0, ET),
                           uncertainty=0.5, impact="장기금리", confirmed=False))
        tsmc = date(y, m, 10)
        if in_win(tsmc):
            out.append(_ev("rule", f"TSMC-{tsmc}", "TSMC 월매출 발표 (예상일·10일경)", at(tsmc, 14, 0, KST),
                           category="corp", tier="sector", uncertainty=0.5, impact="반도체",
                           confirmed=False, tickers=["TSM", "NVDA", "SKHY"]))
        opx = nth_weekday(y, m, 3, 2)
        if opx and in_win(opx):
            out.append(_ev("rule", f"KROPX-{opx}", "韓 옵션만기일(둘째 목요일)", at(opx, 15, 20, KST),
                           uncertainty=0.4, impact="국내 수급·변동성", confirmed=True))
        if m in (1, 4, 7, 10):
            td = nth_business_day(y, m, 1)
            if in_win(td):
                out.append(_ev("rule", f"TSLAdel-{td}", "테슬라 분기 인도량 (예상일·분기 첫 영업일)",
                               at(td, 9, 0, ET), category="corp", tier="mega", uncertainty=0.7,
                               impact="EV·테슬라", confirmed=False, tickers=["TSLA"]))
            pr = nth_business_day(y, m, 3)
            if in_win(pr):
                out.append(_ev("rule", f"KRprelim-{pr}", "삼성전자·SK하이닉스 잠정실적 (예상·분기 첫 주)",
                               at(pr, 8, 30, KST), category="earnings", tier="mega", uncertainty=0.6,
                               impact="반도체·코스피", confirmed=False,
                               tickers=["005930", "000660", "SKHY"]))
        if m in (2, 5, 8, 11):
            # MSCI 분기 리밸런싱 효력: 해당 월 마지막 영업일 (근사)
            last = date(y, m, 28)
            while (last + timedelta(days=1)).month == m:
                last += timedelta(days=1)
            while last.weekday() >= 5:
                last -= timedelta(days=1)
            if in_win(last):
                out.append(_ev("rule", f"MSCI-{last}", "MSCI 분기 리밸런싱 효력 (예상일)", at(last, 15, 30, KST),
                               uncertainty=0.4, impact="외국인 수급", confirmed=False))

    # --- EIA 주간 원유재고: 매주 수요일 10:30 ET ---
    d = start
    while d <= end:
        if d.weekday() == 2:
            out.append(_ev("rule", f"EIA-{d}", "美 EIA 주간 원유재고", at(d, 10, 30, ET),
                           uncertainty=0.4, impact="유가·정유", confirmed=True))
        d += timedelta(days=1)
    return out
