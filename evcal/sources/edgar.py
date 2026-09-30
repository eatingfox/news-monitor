"""SEC EDGAR 공식 데이터 — 관심 US 종목의 8-K/6-K 공시.

TOS: SEC 공개 API. 공정접근 정책상 User-Agent(연락처) 필수, 초당 10요청 이하.
      https://www.sec.gov/os/accessing-edgar-data  → 이 어댑터는 요청 간 0.15s 대기.
Phase 1: 공시 자체를 이벤트로 기록(제출일 기준, lookback_days).
Phase 2: 최근 edgar_parse_lookback_days(기본 45일) 8-K 중 Item 2.02/7.01/8.01 의 본문·EX-99 보도자료를
         받아 '예고된 미래 이벤트'(실적일·인베스터데이·컨퍼런스 참석·제품행사)를 추출(edgar_items.py).
         접수번호별 파싱 결과는 events.db(edgar_parsed)에 캐시 → 매 갱신 때 재요청하지 않음.
"""
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from ..tz import ET, KST, at
from ..db import make_id, connect
from . import edgar_items

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FORMS = {"8-K", "8-K/A", "6-K", "6-K/A"}
PARSE_FORMS = {"8-K", "8-K/A"}
PARSE_ITEMS_DEFAULT = ["2.02", "7.01", "8.01"]
EX99_RE = re.compile(r"ex[-_]?99|ex99|exhibit99", re.I)
MAX_DOC_BYTES = 3_000_000


def _get(url, ua, timeout=25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_DOC_BYTES)


def _get_json(url, ua, timeout=20):
    return json.loads(_get(url, ua, timeout).decode("utf-8"))


def all_watchlist_tickers(cfg) -> list[str]:
    wl = (cfg.get("calendar") or {}).get("watchlist_us") or {}
    out = []
    for group in wl.values():
        out.extend(group)
    return out


def _filing_docs(cik: int, acc_nd: str, primary: str, ua: str) -> list[str]:
    """본문 후보 URL: primary doc + EX-99* (최대 3개)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/"
    urls = [base + primary]
    try:
        idx = _get_json(base + "index.json", ua)
        time.sleep(0.15)
        for it in (idx.get("directory") or {}).get("item") or []:
            n = it.get("name", "")
            if EX99_RE.search(n) and n.lower().endswith((".htm", ".html")) and n != primary:
                urls.append(base + n)
    except Exception:
        pass
    return urls[:3]


def _parse_filing(con, ticker, cik, acc, acc_nd, primary, filed: date, ua, today, end, log):
    """캐시 우선. 없으면 문서 내려받아 파싱 후 캐시(원본 이벤트는 날짜 필터 전 상태로 저장)."""
    row = con.execute("SELECT events FROM edgar_parsed WHERE acc=?", (acc,)).fetchone()
    if row:
        raw = json.loads(row[0])
    else:
        raw = []
        for url in _filing_docs(cik, acc_nd, primary, ua):
            try:
                body = _get(url, ua).decode("utf-8", "ignore")
                time.sleep(0.15)
            except Exception as e:
                log(f"[edgar] {ticker} 문서 실패 {url.rsplit('/', 1)[-1]}: {e}")
                continue
            text = edgar_items.html_to_text(body)
            # 캐시에는 필터 전(공시일 이후 전부)을 저장 → 이후 갱신에서 창(window)만 다시 적용
            raw += edgar_items.extract(ticker, text, filed, filed + timedelta(days=1), filed + timedelta(days=400), url, acc)
        # 동일 이벤트 중복 제거
        uniq = {e["id"]: e for e in raw}
        raw = list(uniq.values())
        con.execute("INSERT OR REPLACE INTO edgar_parsed (acc,ticker,filed,parsed_at,events) VALUES (?,?,?,?,?)",
                    (acc, ticker, filed.isoformat(), datetime.now(timezone.utc).isoformat(),
                     json.dumps(raw, ensure_ascii=False)))
        con.commit()
    out = []
    for e in raw:
        d = datetime.fromisoformat(e["when_kst"]).astimezone(KST).date()
        if today <= d <= end:
            out.append(e)
    return out


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    cal = cfg.get("calendar") or {}
    ua = cal.get("edgar_user_agent") or (cfg.get("btc_treasury") or {}).get("sec_user_agent")
    if not ua:
        log("[edgar] User-Agent 미설정 → 건너뜀")
        return []
    tickers = all_watchlist_tickers(cfg)
    try:
        tmap_raw = _get_json(TICKERS_URL, ua)
    except Exception as e:
        log(f"[edgar] ticker map 실패: {e}")
        return []
    tmap = {}
    for _, v in tmap_raw.items():
        tmap[str(v.get("ticker", "")).upper()] = int(v.get("cik_str"))
    today = date.today()
    lookback_start = today - timedelta(days=int(cal.get("lookback_days", 3)))
    parse_start = today - timedelta(days=int(cal.get("edgar_parse_lookback_days", 45)))
    parse_items = set(cal.get("edgar_parse_items") or PARSE_ITEMS_DEFAULT)
    parse_on = bool(cal.get("edgar_parse_enabled", True))
    con = connect()
    out, parsed_n, fetched = [], 0, 0
    for t in tickers:
        cik = tmap.get(t.upper())
        if not cik:
            continue  # ADR/미등록(예: SKHY) 은 조용히 건너뜀
        try:
            sub = _get_json(SUBMISSIONS_URL.format(cik=cik), ua)
        except Exception as e:
            log(f"[edgar] {t} submissions 실패: {e}")
            continue
        time.sleep(0.15)
        rec = (sub.get("filings") or {}).get("recent") or {}
        forms = rec.get("form", [])
        dates = rec.get("filingDate", [])
        accs = rec.get("accessionNumber", [])
        docs = rec.get("primaryDocument", [])
        items_l = rec.get("items", [""] * len(forms))
        for form, fd, acc, doc, items in zip(forms, dates, accs, docs, items_l):
            if form not in FORMS:
                continue
            try:
                d = date.fromisoformat(fd)
            except ValueError:
                continue
            if d < parse_start or d > end:
                continue
            acc_nd = acc.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/{doc}"
            if d >= lookback_start:
                w = at(d, 16, 30, ET)  # 제출 시각 미상 → 장후로 근사(all_day)
                out.append({
                    "id": make_id("edgar", acc), "name": f"{t} {form} 공시 제출",
                    "category": "corp", "tickers": [t], "when_et": w.isoformat(),
                    "when_kst": w.astimezone(KST).isoformat(), "all_day": True,
                    "tier": None, "uncertainty": 0.7, "impact": "개별종목",
                    "date_confirmed": True, "url": url, "src": "edgar", "is_watchlist": True,
                    "note": f"Item {items}" if items else None,
                })
            # Phase 2: 본문 파싱
            if parse_on and form in PARSE_FORMS:
                its = {x.strip() for x in (items or "").split(",") if x.strip()}
                if its and not (its & parse_items):
                    continue
                cached = con.execute("SELECT 1 FROM edgar_parsed WHERE acc=?", (acc,)).fetchone()
                try:
                    got = _parse_filing(con, t, cik, acc, acc_nd, doc, d, ua, today, end, log)
                except Exception as e:
                    log(f"[edgar] {t} {acc} 파싱 실패: {e}")
                    continue
                parsed_n += 1
                fetched += 0 if cached else 1
                out += got
    n8k = sum(1 for e in out if e.get("src") == "edgar8k")
    log(f"[edgar] {len(out) - n8k}건 공시 + 예고이벤트 {n8k}건 "
        f"(관심종목 {len(tickers)}개 중 CIK 매핑 {sum(1 for t in tickers if t.upper() in tmap)}개, "
        f"8-K 본문 {parsed_n}건 검사/{fetched}건 신규 수신)")
    for e in out:
        if e.get("src") == "edgar8k":
            log(f"[edgar]   ▶ {e['when_kst'][:16]} {e['name']}")
    return out
