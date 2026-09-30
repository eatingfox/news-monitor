"""
시세 조회 — 비트코인 현물가 + 미국 주식 시세(프리/애프터 포함).

무료·키 불필요 소스만 쓴다. 각 소스는 폴백 체인을 갖고, 전부 실패하면
None 을 돌려준다(추정값을 만들어내지 않는다).
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


def _json(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_WEB})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- 비트코인

def btc_usd():
    """(가격, 출처) — 실패 시 (None, 에러문자열들)."""
    errs = []
    try:
        d = _json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        return float(d["data"]["amount"]), "Coinbase"
    except Exception as e:
        errs.append("Coinbase:%s" % e)
    try:
        d = _json("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=bitcoin&vs_currencies=usd")
        return float(d["bitcoin"]["usd"]), "CoinGecko"
    except Exception as e:
        errs.append("CoinGecko:%s" % e)
    try:
        d = _json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
        k = next(iter(d["result"].values()))
        return float(k["c"][0]), "Kraken"
    except Exception as e:
        errs.append("Kraken:%s" % e)
    return None, "; ".join(errs)


# ---------------------------------------------------------------- 주식

_SESSION_KR = {"pre": "프리마켓", "regular": "정규장", "post": "애프터", "closed": "장마감"}


def quote(ticker):
    """야후 차트 API. 프리/애프터 체결이 있으면 그 값을 현재가로 쓴다.

    반환: {price, prev_close, change_pct, session, asof_utc, source} 또는 None.
    (지연 시세다 — 실시간 아님. mNAV 일간 추적 용도에는 충분.)
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=1d&interval=5m&includePrePost=true" % ticker)
    for host in ("query1", "query2"):
        try:
            d = _json(url.replace("query1", host), timeout=20)
            res = d["chart"]["result"][0]
            meta = res["meta"]
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            price = meta.get("regularMarketPrice")
            asof = meta.get("regularMarketTime")

            # 프리/애프터 체결이 정규장 종가보다 최신이면 그쪽을 쓴다.
            ts = res.get("timestamp") or []
            closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
            bars = [(t, c) for t, c in zip(ts, closes) if c is not None]
            if bars and (not asof or bars[-1][0] >= asof):
                asof, price = bars[-1][0], bars[-1][1]

            if price is None:
                continue
            now = int(datetime.now(timezone.utc).timestamp())
            per = meta.get("currentTradingPeriod") or {}
            sess = "closed"
            for name in ("pre", "regular", "post"):
                p = per.get(name) or {}
                if p.get("start") and p.get("end") and p["start"] <= now < p["end"]:
                    sess = name
                    break
            return {
                "ticker": ticker,
                "price": float(price),
                "prev_close": float(prev) if prev else None,
                "change_pct": ((price / prev - 1) * 100) if prev else None,
                "session": _SESSION_KR[sess],
                "asof_utc": datetime.fromtimestamp(asof, timezone.utc).isoformat()
                            if asof else None,
                "source": "Yahoo(%s)" % host,
            }
        except Exception:
            continue
    return None
