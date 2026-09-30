"""
BTC 트레저리 mNAV 추적기 (MSTR / ASST) — 매일 1회 텔레그램 브리핑.

설계 원칙(스펙 그대로):
  주간 : 8-K 감지 -> 본문 파싱(BTC보유·현금·주식수) -> sanity check -> 저장
  일간 : BTC현물가 + 주가 조회 -> NAV / mNAV 계산 -> 임계값 알림 -> 텔레그램 1건

이 도구가 알려주는 건 "구조가 언제 멈추는가"지 "언제 팔아야 하는가"가 아니다.
보유량·주식수는 회사가 공시할 때만 갱신되므로 주중 값은 고정이고, 판단은
공시 직후(월요일 미국시간)에 내리는 게 맞다. 메시지에도 그 취지를 붙인다.

실행:
  python btc_nav.py                 # 공시 점검 + 일간 계산 + 텔레그램 전송
  python btc_nav.py --no-send       # 전송 없이 콘솔 출력
  python btc_nav.py --backfill 10   # 과거 8-K 10건까지 소급 파싱(최초 1회)
  python btc_nav.py --show          # 저장된 주간 이력만 출력
"""
import os
import sys
import json
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import btc_edgar as E
import btc_prices as P

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "btc" / "state.json"
ARCHIVE_DIR = ROOT / "data" / "btc"
LOG_DIR = ROOT / "logs"
KST = ZoneInfo("Asia/Seoul")


def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = "%s %s" % (datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with (LOG_DIR / "btc_nav.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------ 상태 저장

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def issuer_state(st, ticker):
    return st.setdefault(ticker, {"weeks": [], "skipped": [], "failed": {}})


# ------------------------------------------------------------------ 포맷 유틸

def money(v):
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.1fM" % (v / 1e6)
    return "$" + f"{v:,.0f}"


def num(v, dec=0):
    if v is None:
        return "-"
    return f"{v:,.{dec}f}"


def pct(v, dec=1, sign=True):
    if v is None:
        return "-"
    s = "%+.*f%%" if sign else "%.*f%%"
    return s % (dec, v)


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# ------------------------------------------------------------------ 공시 처리

def refresh_filings(cfg_bt, iss, st, limit, notes):
    """새 8-K 를 파싱해 주간 이력에 추가. 파싱/검증 실패는 notes 에 남긴다."""
    ua = cfg_bt["sec_user_agent"]
    tic = iss["ticker"]
    ist = issuer_state(st, tic)
    seen = {w["accession"] for w in ist["weeks"]} | set(ist["skipped"])
    parser = E.PARSERS[tic]

    try:
        filings = E.list_filings(iss["cik"], ua, forms=("8-K",), limit=limit)
    except E.EdgarError as e:
        notes.append("%s: EDGAR 조회 실패 — %s" % (tic, e))
        return 0

    added = 0
    for f in reversed(filings):          # 오래된 것부터 -> 이력 순서 유지
        if f["accession"] in seen:
            continue
        rec, err, used_url = None, None, None
        for url in E.source_doc_urls(f, ua):
            try:
                lines = E.document_text(url, ua)
            except E.EdgarError as e:
                err = str(e)
                continue
            rec, err = parser(lines)
            if rec:
                used_url = url
                break
        if not rec:
            if err and err.startswith(E.NA):
                ist["skipped"].append(f["accession"])   # 트레저리 공시 아님
            else:
                key = f["accession"]
                if key not in ist["failed"]:
                    ist["failed"][key] = {"date": f["date"], "reason": err,
                                          "url": f["url_index"]}
                    notes.append("%s %s 파싱 실패 — %s\n원문: %s"
                                 % (tic, f["date"], err, f["url_index"]))
            continue

        prev = ist["weeks"][-1] if ist["weeks"] else None
        warns, fatal = E.sanity_check(rec, prev)
        rec.update({"accession": f["accession"], "filed": f["date"],
                    "url": used_url, "url_index": f["url_index"],
                    "warnings": warns})
        if fatal:
            rec["unverified"] = True
            notes.append("%s %s 자체검증 실패 — %s\n원문: %s\n(값은 저장하되 '미검증' 표시)"
                         % (tic, f["date"], " / ".join(warns), f["url_index"]))
        elif warns:
            notes.append("%s %s: %s" % (tic, f["date"], " / ".join(warns)))

        ist["weeks"].append(rec)
        added += 1
        log("%s 새 공시 반영: %s (%s 기준) BTC %s, 주간 %s"
            % (tic, f["date"], rec.get("as_of"), num(rec.get("btc_held")),
               num(rec.get("btc_bought"))))

    keep = cfg_bt.get("history_weeks", 26)
    if len(ist["weeks"]) > keep:
        ist["weeks"] = ist["weeks"][-keep:]
    ist["skipped"] = ist["skipped"][-200:]
    return added


# ------------------------------------------------------------------ 주식수

def effective_shares(iss, ist):
    """(주식수, 설명, 추정여부)

    ASST 는 8-K 가 '유효 보통주'를 직접 준다.
    MSTR 은 총 주식수를 8-K 에 싣지 않으므로 10-Q 표지 기준선 + 이후 주간
    ATM 순발행(발행 - 자사주매입) 누적으로 추정한다. 추정임을 반드시 표기한다.
    """
    weeks = ist["weeks"]
    if not weeks:
        return None, "공시 이력 없음", True
    last = weeks[-1]
    if last.get("shares_effective"):
        return last["shares_effective"], "8-K 공시값(%s 기준)" % last.get("as_of"), False

    base = iss.get("shares_baseline") or {}
    if not base.get("shares"):
        return None, "주식수 기준선 미설정 (config.btc_treasury.issuers[].shares_baseline)", True

    b_date = base["as_of"]
    net = 0.0
    used = 0
    for w in weeks:
        # 기준선 날짜 이후에 '시작'한 주간만 더한다(기준선과 겹치는 주는 제외).
        if (w.get("period_start") or "") <= b_date:
            continue
        net += (w.get("shares_sold") or 0) - (w.get("shares_repurchased") or 0)
        used += 1
    total = base["shares"] + net
    desc = ("추정 = %s 기준 %s + 이후 %d주 ATM 순발행 %s"
            % (b_date, num(base["shares"] / 1e6, 1) + "M", used,
               num(net / 1e6, 1) + "M"))
    return total, desc, True


def senior_claims(iss, ist):
    """보통주보다 앞선 청구권(부채 + 우선주 청산우선). (금액, 설명, 기준일)"""
    li = iss.get("liabilities") or {}
    if not li:
        return None, None, None
    debt = li.get("debt_usd")
    pref = li.get("preferred_usd")
    detail = []
    if debt is not None:
        detail.append("부채 %s" % money(debt))

    pf = li.get("preferred_from")
    if pf and ist["weeks"]:
        v = ist["weeks"][-1].get(pf["field"])
        if v:
            pref = v * pf["per_share"]
            detail.append("우선주 %s (%s주 × $%s, 공시값)"
                          % (money(pref), num(v), num(pf["per_share"])))
    elif pref is not None:
        detail.append("우선주 %s" % money(pref))

    if debt is None and pref is None:
        return None, None, None
    total = (debt or 0) + (pref or 0)
    return total, " + ".join(detail), li.get("as_of")


# ------------------------------------------------------------------ 계산

def compute(iss, ist, btc_price, q):
    """한 종목의 NAV/mNAV 등. 계산 불가한 항목은 None 으로 둔다."""
    if not ist["weeks"]:
        return {"ticker": iss["ticker"], "error": "공시 이력 없음"}
    last = ist["weeks"][-1]
    r = {"ticker": iss["ticker"], "name": iss.get("name", ""), "last": last}

    btc_held = last.get("btc_held")
    r["btc_value"] = btc_held * btc_price if (btc_held and btc_price) else None

    # 현금: 최신 공시에 항목이 빠지면(회사가 섹션을 없애면) NAV 가 그만큼 통째로
    # 줄어 mNAV 가 조용히 튄다. 직전 값이 있으면 이월하고 그 사실을 표시한다.
    r["cash"] = last.get("cash_usd")
    if r["cash"] is None:
        for w in reversed(ist["weeks"][:-1]):
            if w.get("cash_usd") is not None:
                r["cash"] = w["cash_usd"]
                r["cash_stale_from"] = w.get("as_of")
                break
    r["other"] = last.get("other_assets_usd")
    r["other_label"] = last.get("other_assets_label")

    if r["btc_value"] is not None:
        r["nav_gross"] = r["btc_value"] + (r["cash"] or 0) + (r["other"] or 0)
    else:
        r["nav_gross"] = None

    shares, s_desc, s_est = effective_shares(iss, ist)
    r["shares"], r["shares_desc"], r["shares_est"] = shares, s_desc, s_est
    r["mktcap"] = shares * q["price"] if (shares and q) else None

    claims, c_desc, c_asof = senior_claims(iss, ist)
    r["claims"], r["claims_desc"], r["claims_asof"] = claims, c_desc, c_asof
    r["nav_net"] = (r["nav_gross"] - claims) if (r["nav_gross"] and claims) else None

    r["mnav_simple"] = (r["mktcap"] / r["nav_gross"]
                        if (r["mktcap"] and r["nav_gross"] and r["nav_gross"] > 0) else None)
    r["mnav_net"] = (r["mktcap"] / r["nav_net"]
                     if (r["mktcap"] and r["nav_net"] and r["nav_net"] > 0) else None)
    # 부채·우선주를 반영한 값이 있으면 그걸 대표값으로 쓴다(자본구조가 큰 회사는
    # 단순 mNAV 가 구조적으로 1 아래로 나와 임계값 판단이 무의미해진다).
    r["mnav"] = r["mnav_net"] or r["mnav_simple"]
    r["mnav_basis"] = "순(부채·우선주 차감)" if r["mnav_net"] else "단순(스펙 기본식)"

    r["avg_cost"] = last.get("btc_avg_cost")
    if r["avg_cost"] and btc_price:
        r["vs_avg_cost"] = (btc_price / r["avg_cost"] - 1) * 100

    # 주간 순매입 추이 (조달 경색 감지)
    flows = [w.get("btc_bought") for w in ist["weeks"] if w.get("btc_bought") is not None]
    r["flow"] = flows[-1] if flows else None
    look = 4
    r["flow_median"] = median(flows[-(look + 1):-1]) if len(flows) >= 2 else None
    r["flow_4w"] = sum(flows[-look:]) if flows else None
    r["flow_4w_n"] = min(look, len(flows))

    # "BTC < 최근 매입 평균단가" 판정용 VWAP.
    # ASST 는 8-K 에 전체 평단을 싣지 않으므로, 이력에 있는 매입 주차들의
    # 거래량가중평균을 쓴다. ('전체 평단'이 아니라 '최근 매입분'임을 표기)
    vw_n, qty, cost = 0, 0.0, 0.0
    for w in reversed(ist["weeks"]):
        f, pr = w.get("btc_bought"), w.get("buy_avg_price")
        if f and f > 0 and pr:
            qty += f
            cost += f * pr
            vw_n += 1
        if vw_n >= 8:
            break
    r["recent_vwap"] = (cost / qty) if qty else None
    r["recent_vwap_n"] = vw_n
    if r["recent_vwap"] and btc_price:
        r["vs_recent_vwap"] = (btc_price / r["recent_vwap"] - 1) * 100

    if last.get("as_of"):
        try:
            age = (date.today() - date.fromisoformat(last["as_of"])).days
            r["data_age_days"] = age
        except ValueError:
            pass
    return r


def build_alerts(r, al):
    """스펙의 알림 조건. (라벨, 문장) 리스트."""
    out = []
    t = r["ticker"]
    m = r.get("mnav")
    if m is not None:
        if m < al["mnav_stop"]:
            out.append(("🛑", "%s mNAV %.2f — 1.00 아래. 주식 발행으로 BTC를 늘리는 "
                              "구조가 멈추는 구간." % (t, m)))
        elif m < al["mnav_warn"]:
            out.append(("⚠️", "%s mNAV %.2f — 경고선(%.2f) 아래."
                        % (t, m, al["mnav_warn"])))

    f, fm = r.get("flow"), r.get("flow_median")
    if f is not None:
        if f < 0:
            out.append(("🛑", "%s 지난주 BTC 순매도 %s개. 매입이 아니라 처분 국면."
                        % (t, num(abs(f)))))
        elif fm and fm > 0 and f < fm * al["buy_slowdown_ratio"]:
            out.append(("⚠️", "%s 주간 매입 %s개 — 직전 4주 중앙값(%s)의 %.0f%% 미만. 조달 경색 신호."
                        % (t, num(f), num(fm), al["buy_slowdown_ratio"] * 100)))
    # 중앙값이 0/음수인 국면에선 비율 규칙이 무의미해진다. 누적으로도 본다.
    f4 = r.get("flow_4w")
    if f4 is not None and r.get("flow_4w_n", 0) >= 3 and f4 <= 0:
        out.append(("🛑", "%s 최근 %d주 누적 %s BTC — 매입이 멈췄거나 순처분 중."
                    % (t, r["flow_4w_n"], num(f4))))

    v = r.get("vs_avg_cost")
    if v is not None and v < 0:
        out.append(("⚠️", "%s BTC 현재가가 전체 평균단가($%s)보다 %.1f%% 낮음."
                    % (t, num(r["avg_cost"]), abs(v))))
    v2 = r.get("vs_recent_vwap")
    if v2 is not None and v2 < 0:
        out.append(("⚠️", "%s BTC 현재가가 최근 %d주 매입평단($%s)보다 %.1f%% 낮음."
                    % (t, r.get("recent_vwap_n", 0), num(r["recent_vwap"]), abs(v2))))

    age = r.get("data_age_days")
    if age is not None and age > al.get("stale_days", 10):
        out.append(("⚠️", "%s 공시 데이터가 %d일 경과 — 주간 8-K가 끊겼는지 확인 필요." % (t, age)))
    return out


# ------------------------------------------------------------------ 메시지

def render(cfg_bt, results, btc_price, btc_src, quotes, notes):
    now = datetime.now(KST)
    al = cfg_bt["alerts"]
    L = ["₿ BTC 트레저리 mNAV — %s(%s) %s KST"
         % (now.strftime("%Y-%m-%d"), "월화수목금토일"[now.weekday()],
            now.strftime("%H:%M"))]
    L.append("BTC $%s  (%s)" % (num(btc_price) if btc_price else "?", btc_src))
    L.append("")

    alerts = []
    for r in results:
        tic = r["ticker"]
        q = quotes.get(tic)
        if r.get("error"):
            L.append("▸ %s — %s" % (tic, r["error"]))
            L.append("")
            continue

        head = "▸ %s%s" % (tic, " (선행지표)" if r.get("lead") else "")
        if q:
            head += "  $%s  %s  %s" % (num(q["price"], 2), pct(q.get("change_pct")),
                                       q["session"])
        else:
            head += "  주가 조회 실패"
        L.append(head)

        if r.get("mnav") is not None:
            line = "  mNAV %.2f  [%s]" % (r["mnav"], r["mnav_basis"])
            if r.get("mnav_net") and r.get("mnav_simple"):
                line += "   (단순식 %.2f)" % r["mnav_simple"]
            L.append(line)
        else:
            L.append("  mNAV 계산 불가 (주식수 또는 시세 없음)")

        parts = ["BTC %s" % money(r["btc_value"])]
        if r.get("cash"):
            parts.append("현금 %s%s" % (money(r["cash"]),
                                      "(%s 이월)" % r["cash_stale_from"]
                                      if r.get("cash_stale_from") else ""))
        if r.get("other"):
            parts.append("%s %s" % (r.get("other_label") or "기타", money(r["other"])))
        L.append("  NAV(총) %s = %s" % (money(r["nav_gross"]), " + ".join(parts)))
        if r.get("nav_net"):
            L.append("  NAV(순) %s  ← %s 차감 (%s 기준)"
                     % (money(r["nav_net"]), money(r["claims"]), r.get("claims_asof")))

        last = r["last"]
        L.append("  보유 %s BTC" % num(last.get("btc_held")))
        if r.get("avg_cost"):
            L.append("  전체 평단 $%s → 현재가 %s"
                     % (num(r["avg_cost"]), pct(r.get("vs_avg_cost"))))
        if r.get("recent_vwap"):
            L.append("  최근 %d주 매입평단 $%s → 현재가 %s"
                     % (r.get("recent_vwap_n", 0), num(r["recent_vwap"]),
                        pct(r.get("vs_recent_vwap"))))
        flow = r.get("flow")
        ftxt = ("매도 %s" % num(abs(flow))) if (flow or 0) < 0 else \
               ("매입 %s" % num(flow)) if flow else "변동 없음"
        f4 = r.get("flow_4w")
        f4txt = ("  ·  최근 %d주 누적 %s" % (r["flow_4w_n"], num(f4))) if f4 is not None else ""
        L.append("  주간 %s%s  [%s 공시 · %s 기준]"
                 % (ftxt, f4txt, last.get("filed"), last.get("as_of")))
        L.append("  주식수 %sM  (%s)" % (num((r["shares"] or 0) / 1e6, 1),
                                       r.get("shares_desc")))
        if last.get("unverified"):
            L.append("  ❗이 주차 값은 자체검증 실패 — 원문 확인 필요")
        L.append("")
        alerts += [(lab, txt) for lab, txt in build_alerts(r, al)]

    if alerts:
        L.append("── 알림 ──")
        for lab, txt in alerts:
            L.append("%s %s" % (lab, txt))
        L.append("")
    else:
        L.append("── 알림 없음 (임계값 미도달) ──")
        L.append("")

    if notes:
        L.append("── 데이터 경고 ──")
        for n in notes:
            L.append("• " + n)
        L.append("")

    L.append("※ 보유량·주식수는 주 1회 8-K 로만 갱신됩니다. 주 후반일수록 실제보다")
    L.append("  낮게 잡히니, 판단은 월요일 공시 직후 값으로 하세요.")
    L.append("※ 이 지표는 '구조가 언제 멈추는가'를 봅니다. 청산 타이밍용이 아닙니다.")
    return "\n".join(L)


# ------------------------------------------------------------------ 전송

def telegram_send(cfg, text):
    tg = cfg.get("telegram") or {}
    token = (tg.get("bot_token") or "").strip()
    chat_id = str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        log("[ERROR] telegram 토큰/chat_id 없음")
        return False
    ok = True
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]:
        payload = json.dumps({"chat_id": chat_id, "text": chunk,
                              "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if not json.loads(r.read().decode("utf-8")).get("ok"):
                    ok = False
        except Exception as e:
            log("[ERROR] telegram 전송 실패: %s" % e)
            ok = False
    return ok


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true", help="전송 없이 콘솔 출력")
    ap.add_argument("--backfill", type=int, default=None,
                    help="8-K 를 N건까지 소급해 파싱(최초 1회)")
    ap.add_argument("--show", action="store_true", help="저장된 주간 이력만 출력")
    args = ap.parse_args()

    from secrets_loader import load_config
    cfg = load_config()
    bt = cfg.get("btc_treasury") or {}
    if not bt.get("enabled"):
        log("btc_treasury.enabled=false → 건너뜀")
        return
    st = load_state()

    if args.show:
        for iss in bt["issuers"]:
            ist = issuer_state(st, iss["ticker"])
            print("=" * 64, iss["ticker"])
            for w in ist["weeks"]:
                print(" %s (기준 %s) BTC %s 주간 %s 현금 %s 주식 %s%s"
                      % (w["filed"], w.get("as_of"), num(w.get("btc_held")),
                         num(w.get("btc_bought")), money(w.get("cash_usd")),
                         num(w.get("shares_effective")),
                         "  [미검증]" if w.get("unverified") else ""))
        return

    notes = []
    limit = args.backfill or bt.get("poll_filings", 6)
    for iss in bt["issuers"]:
        if args.backfill:
            # 파서를 고친 뒤 재시도할 수 있게, 소급 실행 때는 실패 목록을 비운다.
            issuer_state(st, iss["ticker"])["failed"] = {}
        refresh_filings(bt, iss, st, limit, notes)
    save_state(st)

    btc_price, btc_src = P.btc_usd()
    if btc_price is None:
        notes.append("비트코인 시세 조회 전 소스 실패 — %s" % btc_src)
        btc_src = "조회 실패"

    quotes, results = {}, []
    for iss in bt["issuers"]:
        tic = iss["ticker"]
        q = P.quote(tic)
        if q is None:
            notes.append("%s 주가 조회 실패 (Yahoo)" % tic)
        quotes[tic] = q
        r = compute(iss, issuer_state(st, tic), btc_price, q)
        r["lead"] = bool(iss.get("lead"))
        results.append(r)

    # 선행지표(MSTR)를 먼저 배치 — 유동성이 크므로 ASST 보다 앞서 움직인다.
    results.sort(key=lambda x: (not x.get("lead"), x["ticker"]))

    msg = render(bt, results, btc_price, btc_src, quotes, notes)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M")
    (ARCHIVE_DIR / ("brief_%s.txt" % stamp)).write_text(msg, encoding="utf-8")

    if args.no_send:
        print("\n" + "=" * 64 + "\n" + msg + "\n" + "=" * 64)
        log("no-send 모드 → 콘솔 출력만 (%d자)" % len(msg))
        return
    ok = telegram_send(cfg, msg)
    log("전송 %s (%d자)" % ("성공" if ok else "실패", len(msg)))


if __name__ == "__main__":
    main()
