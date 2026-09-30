"""
EDGAR 공시 수집·파싱 (BTC 트레저리 기업).

역할: 회사별 8-K 목록 폴링 -> 본문 htm 텍스트화 -> 이슈어별 파서로 숫자 추출
      -> sanity check. 실패는 조용히 넘기지 않고 reason 을 담아 반환한다.

[스펙 대비 정정] 원래 설계는 "EX-99.1 보도자료 첨부"를 파싱 대상으로 봤으나,
2026-08 현재 MSTR·ASST 둘 다 EX-99.1 이 없고 8-K 기본문서(iXBRL htm) 본문
표에 수치가 들어 있다. 따라서 기본 대상은 primaryDocument 이고, EX-99 계열
첨부가 존재하면 그쪽을 우선 후보로 함께 본다(형식 회귀 대비).
"""
import re
import html
import json
import time
import gzip
import urllib.error
import urllib.request
from datetime import datetime

SEC_HOST = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
_LAST_CALL = [0.0]
_MIN_INTERVAL = 0.25  # SEC 권고(10 req/s) 보다 훨씬 보수적으로


class EdgarError(Exception):
    pass


# 파서가 "이 공시는 트레저리 주간 업데이트가 아님"을 알릴 때 쓰는 접두사.
# (합병·증권발행 등 다른 8-K 는 실패가 아니라 그냥 건너뛸 대상이다.)
NA = "[N/A] "


def _get(url, user_agent, timeout=40):
    """SEC 요청. User-Agent 에 연락처가 없으면 403 이 난다."""
    gap = time.time() - _LAST_CALL[0]
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "text/html,application/json,*/*",
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                _LAST_CALL[0] = time.time()
                return raw
        except urllib.error.HTTPError as e:
            _LAST_CALL[0] = time.time()
            if e.code in (403, 404):
                raise EdgarError("HTTP %d %s" % (e.code, url)) from e
            last = e
        except Exception as e:  # 네트워크 일시 장애
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise EdgarError("요청 실패 %s: %s" % (url, last))


# ---------------------------------------------------------------- 공시 목록

def list_filings(cik, user_agent, forms=("8-K",), limit=20):
    """최근 공시 목록(최신순)."""
    cik10 = str(int(cik)).zfill(10)
    data = json.loads(_get("%s/submissions/CIK%s.json" % (SEC_DATA, cik10), user_agent))
    rec = data["filings"]["recent"]
    items = rec.get("items") or [""] * len(rec["form"])
    out = []
    for i in range(len(rec["form"])):
        form = rec["form"][i]
        if forms and not any(form == f or form.startswith(f + "/") for f in forms):
            continue
        acc = rec["accessionNumber"][i]
        accn = acc.replace("-", "")
        base = "%s/Archives/edgar/data/%d/%s" % (SEC_HOST, int(cik), accn)
        out.append({
            "company": data.get("name", ""),
            "form": form,
            "date": rec["filingDate"][i],
            "accession": acc,
            "primary_doc": rec["primaryDocument"][i],
            "items": items[i],
            "url_dir": base,
            "url_index": "%s/%s-index.html" % (base, acc),
        })
        if len(out) >= limit:
            break
    return out


def filing_documents(filing, user_agent):
    """공시 index.html 의 문서 테이블 -> [{seq, desc, name, type}]"""
    raw = _get(filing["url_index"], user_agent).decode("utf-8", "replace")
    docs = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        row = m.group(1)
        if "Archives" not in row:
            continue
        cells = [" ".join(re.sub(r"<[^>]+>", " ", c).split())
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        name = None
        for m2 in re.finditer(r'href="([^"]+)"', row):
            if "/Archives/" in m2.group(1):
                name = m2.group(1).rsplit("/", 1)[-1]
                break
        if not name:
            continue
        docs.append({
            "seq": cells[0] if cells else "",
            "desc": cells[1] if len(cells) > 1 else "",
            "name": name,
            "type": cells[3] if len(cells) > 3 else "",
        })
    return docs


def document_text(url, user_agent):
    """htm -> 셀/문단 단위로 개행된 평문. 표를 '줄 시퀀스'로 보존하는 게 핵심."""
    raw = _get(url, user_agent).decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(td|th|tr|p|div|li|h\d)>", " \n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = t.replace(" ", " ").replace("’", "'")
    t = re.sub(r"[ \t]+", " ", t)
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def source_doc_urls(filing, user_agent):
    """수치가 실릴 만한 문서 URL 후보. EX-99 계열 우선, 그다음 8-K 기본문서."""
    urls = []
    try:
        for d in filing_documents(filing, user_agent):
            typ = (d.get("type") or "").upper()
            if typ.startswith("EX-99") and d["name"].lower().endswith((".htm", ".html")):
                urls.append("%s/%s" % (filing["url_dir"], d["name"]))
    except EdgarError:
        pass
    urls.append("%s/%s" % (filing["url_dir"], filing["primary_doc"]))
    return urls


# ---------------------------------------------------------------- 숫자 유틸

_DASH = {"-", "—", "–", "‑", "*", "―"}


def _num(tok):
    """'1,110' -> 1110.0 / '-' -> 0.0 / '(1,106)' -> -1106.0 / 그 외 None

    '1,363 (1)' 처럼 각주 번호가 붙은 값과 '846,000*' 같은 별표도 받는다.
    (괄호가 토큰 전체를 감싸면 음수, 값 뒤에 붙으면 각주다 - 구분해야 한다.)
    """
    s = tok.strip()
    s = re.sub(r"\s*\(\d{1,2}\)\s*$", "", s) if re.match(r"^[^(].*\(\d{1,2}\)\s*$", s) else s
    s = s.rstrip("*").strip().strip("$").strip()
    if s in _DASH:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.replace(",", "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        v = float(s)
        return -v if neg else v
    return None


def _values_after(lines, idx, count, lookahead=40):
    """idx 다음 줄부터 '$'/'%' 는 건너뛰며 숫자 토큰을 count 개 모은다."""
    vals = []
    for ln in lines[idx + 1: idx + 1 + lookahead]:
        if ln in ("$", "%"):
            continue
        v = _num(ln)
        if v is None:
            if vals:      # 표 행이 끝났다
                break
            continue      # 값 시작 전의 잡음은 건너뜀
        vals.append(v)
        if len(vals) >= count:
            break
    return vals


def _find(lines, pattern, start=0, flags=re.I):
    rx = re.compile(pattern, flags)
    for i in range(start, len(lines)):
        if rx.search(lines[i]):
            return i
    return -1


def _parse_us_date(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- 파서: MSTR

def _mstr_flow_sign(header):
    """1열 헤더 문자열로 매수/매도 부호를 정한다.

    [중요] 헤더는 주마다 바뀐다: "BTC Purchased / (Sold)", "BTC Sold",
    "BTC Acquired". 위치만 믿고 읽으면 매도를 매수로 뒤집어 읽는다.
    2026-08 현재 MSTR 은 프리퍼드 배당·STRC 자사주 재원으로 BTC 를 매도 중이다.
    """
    low = header.lower()
    buy = any(w in low for w in ("purchas", "acquir", "bought", "added"))
    sell = any(w in low for w in ("sold", "sale", "dispos"))
    if buy and sell:
        return 1.0        # "Purchased / (Sold)" - 괄호가 곧 음수
    if sell:
        return -1.0       # "BTC Sold" - 양수로 적히지만 매도
    if buy:
        return 1.0
    return None


def _mstr_btc_blocks(lines):
    """BTC 표 블록들을 구조로 읽는다. 두 가지 레이아웃을 모두 지원한다.

    (A) 통합형 - 헤더 6개 뒤 값 6개
        BTC Sold | Agg Sale Price | Avg Sale Price
        | Aggregate BTC Holdings | Agg Purchase Price($B) | Avg Purchase Price
    (B) 분할형 - 분기말 주간(예: 2026-07-06)에 쓰인다. 한 공시에 기간 블록이
        둘 이상 들어가고, 각 블록이 [거래 헤더3+값3] + [보유 헤더3+값3] 이다.
        이걸 못 읽으면 그 주가 통째로 누락되고 다음 주에 보유량 불연속이 난다.
    """
    blocks = []
    for h, ln in enumerate(lines):
        if not (re.match(r"^BTC\b", ln, re.I)
                and re.search(r"sold|acquir|purchas|bought", ln, re.I)):
            continue
        sign = _mstr_flow_sign(ln)
        if sign is None or h + 3 >= len(lines):
            continue
        if re.search(r"Aggregate\s+BTC\s+Holdings", lines[h + 3], re.I):
            vals = _values_after(lines, h + 5, 6)      # (A)
            if len(vals) < 6:
                return None, "BTC 표(통합형) 값 부족(%d/6): %s" % (len(vals), vals)
            flow_v, hold_v, hold_i = vals[:3], vals[3:], h + 3
        else:
            flow_v = _values_after(lines, h + 2, 3)    # (B)
            hold_i = _find(lines, r"Aggregate\s+BTC\s+Holdings", start=h + 3)
            if len(flow_v) < 3 or hold_i < 0:
                continue
            hold_v = _values_after(lines, hold_i + 2, 3)
            if len(hold_v) < 3:
                return None, "BTC 표(분할형) 보유값 부족: %s" % hold_v

        b = {"header": ln, "sign": sign,
             "flow": sign * flow_v[0], "flow_abs": flow_v[0],
             "flow_cost_usd": flow_v[1] * 1e6, "flow_avg_price": flow_v[2] or None,
             "held": hold_v[0], "cost_basis_usd": hold_v[1] * 1e9,
             "avg_cost": hold_v[2]}
        # 기준일 / 기간은 헤더 위쪽에서 가장 가까운 것을 쓴다.
        for k in range(hold_i, max(-1, hold_i - 12), -1):
            m = re.match(r"^As of\s+([A-Z][a-z]+ \d{1,2}, \d{4})", lines[k])
            if m:
                b["as_of"] = _parse_us_date(m.group(1))
                break
        for k in range(h, max(-1, h - 12), -1):
            m = re.match(r"^During Period\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+to\s+"
                         r"([A-Z][a-z]+ \d{1,2}, \d{4})", lines[k])
            if m:
                b["period_start"] = _parse_us_date(m.group(1))
                b["period_end"] = _parse_us_date(m.group(2))
                break
        blocks.append(b)
    return blocks, None


def parse_mstr(lines):
    """Strategy Inc 주간 8-K."""
    out = {"issuer": "MSTR"}
    txt = "\n".join(lines)

    # 데이터 앵커는 "Aggregate BTC Holdings" 자체다. 소제목("BTC Update(s)")은
    # 주마다 표기가 달라져 앵커로 쓰기에 위험하다.
    if _find(lines, r"Aggregate\s+BTC\s+Holdings") < 0:
        return None, NA + "BTC 보유 표 없음 (트레저리 주간 공시 아님)"

    blocks, err = _mstr_btc_blocks(lines)
    if err:
        return None, err
    if not blocks:
        return None, "BTC 보유 표는 있으나 블록 해석 실패"

    last = blocks[-1]
    out["blocks"] = len(blocks)
    out["flow_header"] = last["header"]
    out["btc_bought"] = sum(b["flow"] for b in blocks)   # 여러 블록이면 합산
    out["btc_held"] = last["held"]
    out["btc_cost_basis_usd"] = last["cost_basis_usd"]
    out["btc_avg_cost"] = last["avg_cost"]
    out["as_of"] = last.get("as_of")
    out["period_start"] = blocks[0].get("period_start")
    out["period_end"] = last.get("period_end") or last.get("as_of")

    # 거래단가: 블록별 거래대금/수량의 가중평균(매수분·매도분 분리)
    for side, want in (("buy", 1.0), ("sell", -1.0)):
        qty = sum(b["flow_abs"] for b in blocks if b["sign"] == want)
        cost = sum(b["flow_cost_usd"] for b in blocks if b["sign"] == want)
        out[side + "_avg_price"] = (cost / qty) if qty else None
    out["flow_cost_usd"] = sum(b["flow_cost_usd"] for b in blocks)

    # 자체 검산: 총매입원가/보유량 ~= 평균단가, 거래대금/수량 ~= 거래단가
    bad = []
    if out["btc_held"] and out["btc_avg_cost"]:
        implied = out["btc_cost_basis_usd"] / out["btc_held"]
        if abs(implied / out["btc_avg_cost"] - 1) > 0.01:
            bad.append("평균단가 검산 불일치(%s vs %s)"
                       % (f"{implied:,.0f}", f"{out['btc_avg_cost']:,.0f}"))
    for b in blocks:
        if b["flow_abs"] and b["flow_avg_price"]:
            implied = b["flow_cost_usd"] / b["flow_abs"]
            if abs(implied / b["flow_avg_price"] - 1) > 0.01:
                bad.append("거래단가 검산 불일치(%s vs %s)"
                           % (f"{implied:,.0f}", f"{b['flow_avg_price']:,.0f}"))
    if bad:
        return None, "; ".join(bad)

    # USD Reserve / USD Cash (2026-06 이후 신설 -> 없을 수 있음)
    cash, got = 0.0, []
    for label in ("USD Reserve", "USD Cash"):
        m = re.search(label + r"\s*:?\s*\$\s*([\d,.]+)\s*(billion|million)", txt, re.I)
        if m:
            mult = 1e9 if m.group(2).lower() == "billion" else 1e6
            v = float(m.group(1).replace(",", "")) * mult
            out[label.lower().replace(" ", "_") + "_usd"] = v
            cash += v
            got.append(label)
    out["cash_usd"] = cash if got else None
    out["cash_source"] = " + ".join(got) if got else None

    # ATM: 이번 주 보통주 순증(발행 - 자사주매입). 주식수 누적 추정에 쓴다.
    out["shares_sold"] = _mstr_row(lines, "ATM Update", r"^MSTR Stock")
    out["shares_repurchased"] = _mstr_row(lines, "Repurchase Program Updates",
                                          r"^MSTR Stock")
    return out, None


def _mstr_row(lines, section, row_pat):
    i_sec = _find(lines, re.escape(section))
    if i_sec < 0:
        return None
    for i in range(i_sec, min(len(lines), i_sec + 90)):
        if re.match(row_pat, lines[i].strip(), re.I):
            v = _values_after(lines, i, 1, lookahead=4)
            return v[0] if v else None
    return None


# ---------------------------------------------------------------- 파서: ASST

_ASST_ROWS = [
    ("cash_usd",         r"^Cash and cash equivalents", 1e3),
    ("strc_value_usd",   r"^Fair value of STRC Stock", 1e3),
    ("strc_shares",      r"^Shares of STRC held", 1),
    ("btc_held",         r"^Bitcoin held", 1),
    ("class_a_shares",   r"^Class A common stock$", 1),
    ("class_b_shares",   r"^Class B common stock$", 1),
    ("shares_effective", r"^Effective Common Shares Outstanding", 1),
    ("shares_diluted",   r"^Assumed Fully Diluted Shares", 1),
    ("sata_shares",      r"^SATA Stock$", 1),
]


def parse_asst(lines):
    """Strive, Inc 주간 8-K. 표가 [직전값 | 현재값 | 변화] 3열."""
    out = {"issuer": "ASST"}
    txt = "\n".join(lines)

    m = re.search(r"during the period from\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+"
                  r"([A-Z][a-z]+ \d{1,2}, \d{4})", txt, re.I)
    if m:
        out["period_start"] = _parse_us_date(m.group(1))
        out["period_end"] = _parse_us_date(m.group(2))

    m = re.search(r"purchased\s+([\d,]+)\s+bitcoin at an average price of "
                  r"approximately\s+\$\s*([\d,.]+)", txt, re.I)
    if m:
        out["btc_bought"] = _num(m.group(1))
        out["buy_avg_price"] = _num(m.group(2))
    elif re.search(r"did not purchase any bitcoin|no bitcoin (was|were) purchased", txt, re.I):
        out["btc_bought"] = 0.0

    asofs = re.findall(r"As of\s+([A-Z][a-z]+ \d{1,2}, \d{4})", txt)
    if len(asofs) > 1:
        out["as_of"] = _parse_us_date(asofs[1])
    elif asofs:
        out["as_of"] = _parse_us_date(asofs[0])
    else:
        out["as_of"] = out.get("period_end")

    missing = []
    for key, pat, mult in _ASST_ROWS:
        i = _find(lines, pat)
        if i < 0:
            missing.append(key)
            continue
        vals = _values_after(lines, i, 3, lookahead=10)
        if len(vals) < 2:
            missing.append(key)
            continue
        out[key] = vals[1] * mult              # 현재값
        out[key + "_prev"] = vals[0] * mult    # 직전값
    if len(missing) == len(_ASST_ROWS) and "btc_bought" not in out:
        return None, NA + "보유현황 표 없음 (트레저리 공시 아님)"

    # 2026-07-20 이전 형식에는 "Effective Common Shares Outstanding" 행이 없다.
    # 정의가 ClassA + ClassB 이므로 그 경우 직접 합산한다.
    if ("shares_effective" in missing and out.get("class_a_shares")
            and out.get("class_b_shares")):
        out["shares_effective"] = out["class_a_shares"] + out["class_b_shares"]
        prev = ((out.get("class_a_shares_prev") or 0)
                + (out.get("class_b_shares_prev") or 0))
        if prev:
            out["shares_effective_prev"] = prev
        out["shares_effective_derived"] = True
        missing.remove("shares_effective")
    if "shares_diluted" in missing:
        missing.remove("shares_diluted")      # 구형식엔 없는 행 - 필수 아님

    if "btc_held" in missing or "shares_effective" in missing:
        return None, "필수 행 파싱 실패: %s" % missing
    if missing:
        out["_missing"] = missing

    # 자체 검산 1: 유효보통주 = ClassA + ClassB
    a, b, eff = out.get("class_a_shares"), out.get("class_b_shares"), out.get("shares_effective")
    if a and b and eff and abs((a + b) - eff) > 1:
        return None, ("검산 불일치: A(%s)+B(%s) != 유효(%s)"
                      % (f"{a:,.0f}", f"{b:,.0f}", f"{eff:,.0f}"))

    # 자체 검산 2: 문장상 매입량 vs 표의 보유량 증감
    if out.get("btc_held") is not None and out.get("btc_held_prev") is not None:
        delta = out["btc_held"] - out["btc_held_prev"]
        if out.get("btc_bought") is not None and abs(delta - out["btc_bought"]) > 1:
            out["_warn"] = ("문장상 매입 %s vs 표 증감 %s 불일치"
                            % (f"{out['btc_bought']:,.0f}", f"{delta:,.0f}"))
        if out.get("btc_bought") is None:
            out["btc_bought"] = delta

    out["other_assets_usd"] = out.get("strc_value_usd")
    out["other_assets_label"] = "STRC 보유 평가액"
    return out, None


PARSERS = {"MSTR": parse_mstr, "ASST": parse_asst}


# ---------------------------------------------------------------- sanity check

def sanity_check(cur, prev):
    """스펙의 자체 검증. (경고 리스트, 치명여부) 반환.
    치명 = 값을 믿을 수 없으니 사람이 원문을 봐야 하는 상태."""
    warns, fatal = [], False
    if not cur.get("btc_held"):
        return ["보유 BTC 값 없음/0"], True

    if prev:
        pb, cb = prev.get("btc_held"), cur["btc_held"]
        if pb:
            # 공시상 이번주 순증감 vs 보유량 실제 증감. 부호 오독·표 오정렬을
            # 잡는 가장 강한 검증이므로 불일치는 치명으로 본다.
            flow = cur.get("btc_bought")
            if flow is not None and abs((cb - pb) - flow) > 2:
                warns.append("주간 증감 불일치: 공시 %s vs 보유량 변화 %s"
                             % (f"{flow:+,.0f}", f"{cb - pb:+,.0f}"))
                fatal = True
            if cb < pb - 0.5:
                # 공시된 순매도량과 일치하면 '파싱 실패'가 아니라 실제 처분이다.
                # (2026-08 MSTR 이 프리퍼드 배당 재원으로 BTC 를 팔고 있다.)
                explained = (flow is not None and flow < 0
                             and abs((cb - pb) - flow) <= 2)
                warns.append("보유 BTC 감소: %s -> %s (%s)"
                             % (f"{pb:,.0f}", f"{cb:,.0f}",
                                "공시된 매도와 일치" if explained
                                else "매도 공시 없음 - 파싱 실패 의심"))
                if not explained:
                    fatal = True
            elif cb > pb * 1.5:
                warns.append("보유 BTC 이상 급증: %s -> %s (+50%%↑)"
                             % (f"{pb:,.0f}", f"{cb:,.0f}"))
                fatal = True
        ps, cs = prev.get("shares_effective"), cur.get("shares_effective")
        if ps and cs:
            if cs < ps * 0.995:
                warns.append("유효 보통주 감소: %s -> %s (자사주 매입 또는 파싱 실패)"
                             % (f"{ps:,.0f}", f"{cs:,.0f}"))
                fatal = True
            elif cs < ps:
                warns.append("유효 보통주 소폭 감소: %s -> %s"
                             % (f"{ps:,.0f}", f"{cs:,.0f}"))
            elif cs > ps * 1.5:
                warns.append("유효 보통주 이상 급증: %s -> %s (+50%%↑)"
                             % (f"{ps:,.0f}", f"{cs:,.0f}"))
                fatal = True

    if cur.get("buy_avg_price") and cur.get("btc_avg_cost"):
        r = cur["buy_avg_price"] / cur["btc_avg_cost"]
        if not (0.2 < r < 5):
            warns.append("이번주 매입단가가 평균단가 대비 비정상 (파싱 의심)")
            fatal = True
    if cur.get("_warn"):
        warns.append(cur["_warn"])
    return warns, fatal
