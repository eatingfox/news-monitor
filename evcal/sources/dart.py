"""DART OpenAPI(공식) — 관심 KR 종목 공시(재공시 예정 포함).

TOS: 공식 API(무료 인증키). https://opendart.fss.or.kr  키 없으면 건너뜀.
"""
import io
import os
import json
import zipfile
import urllib.request
import xml.etree.ElementTree as ET_
from pathlib import Path
from datetime import date, timedelta

from ..tz import KST, ET, at
from ..db import make_id

CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={key}"
LIST_URL = ("https://opendart.fss.or.kr/api/list.json?crtfc_key={key}&corp_code={corp}"
            "&bgn_de={bgn}&end_de={end}&page_count=50")
# 제외할 일상 공시 키워드(증권사 ELS/DLS·일괄신고류). 수주·해명·IR·주요사항 등은 유지.
DART_NOISE = ("증권발행실적보고서", "일괄신고추가서류", "일괄신고서", "투자설명서")


def _corp_map(key, log):
    """stock_code → corp_code"""
    req = urllib.request.Request(CORPCODE_URL.format(key=key), headers={"User-Agent": "news-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read(z.namelist()[0])
    root = ET_.fromstring(xml)
    m = {}
    for el in root.findall("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code")
    return m


def resolve_key(cal) -> str:
    """config.calendar.dart_api_key → 환경변수 DART_API_KEY → dart_api_key_file 순.
    (기본값: secrets/dart_api_key.txt)"""
    k = (cal.get("dart_api_key") or "").strip()
    if k:
        return k
    k = os.environ.get("DART_API_KEY", "").strip()
    if k:
        return k
    p = cal.get("dart_api_key_file")
    if p and Path(p).exists():
        return Path(p).read_text(encoding="utf-8").strip()
    return ""


def collect(cfg, start: date, end: date, log=print) -> list[dict]:
    cal = cfg.get("calendar") or {}
    key = resolve_key(cal)
    if not key:
        log("[dart] 인증키 없음 → 건너뜀 (config.calendar.dart_api_key / DART_API_KEY / dart_api_key_file)")
        return []
    wl = cal.get("watchlist_kr") or {}
    try:
        cmap = _corp_map(key, log)
    except Exception as e:
        log(f"[dart] corpCode 실패: {e}")
        return []
    bgn = (date.today() - timedelta(days=int(cal.get("lookback_days", 3)))).strftime("%Y%m%d")
    endd = end.strftime("%Y%m%d")
    out = []
    for name, sc in wl.items():
        corp = cmap.get(sc)
        if not corp:
            continue
        try:
            req = urllib.request.Request(LIST_URL.format(key=key, corp=corp, bgn=bgn, end=endd),
                                         headers={"User-Agent": "news-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                js = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log(f"[dart] {name} list 실패: {e}")
            continue
        for it in js.get("list", []):
            rn = it.get("report_nm", "")
            rd = it.get("rcept_dt", "")
            no = it.get("rcept_no", "")
            # 증권사 일상 공시(ELS/DLS 발행·일괄신고류)는 노이즈 → 제외
            if any(k in rn for k in DART_NOISE):
                continue
            try:
                d = date(int(rd[:4]), int(rd[4:6]), int(rd[6:8]))
            except Exception:
                continue
            w = at(d, 9, 0, KST)
            flag = "재공시/예정" if ("예정" in rn or "재공시" in rn or "정정" in rn) else "공시"
            out.append({
                "id": make_id("dart", no), "name": f"{name} {flag}: {rn[:40]}",
                "category": "kr_dart", "tickers": [sc], "when_et": w.astimezone(ET).isoformat(),
                "when_kst": w.isoformat(), "all_day": True, "tier": None, "uncertainty": 0.7,
                "impact": "개별종목", "date_confirmed": True,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}", "src": "dart",
                "is_watchlist": True,
            })
    log(f"[dart] {len(out)}건")
    return out
