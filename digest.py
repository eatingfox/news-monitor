"""
News Monitor — 오전/저녁 정기 다이제스트 (Stage 3, 종합 브리핑).

직전 window_hours(기본 12h) 구간의 수집 기사를 모아, 한국 개인투자자용 시황
브리핑 프롬프트로 `claude -p` 를 호출해 텔레그램 1건으로 종합 전송한다.
data/news/*.md 에서 기사를 읽으므로 브라우저/세션 불필요.

수동 실행:  python digest.py            # 지금 시각 기준 직전 12h
           python digest.py --hours 24 # 구간 길이 지정
스케줄:     08:00 -> "오전",  20:00 -> "저녁"  (실행 시각으로 라벨 결정)
"""
import os
import re
import sys
import json
import argparse
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "news"
DIGEST_DIR = ROOT / "data" / "digests"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"
KST = ZoneInfo("Asia/Seoul")

# 감시 목록 (★ 강조 기준) — 필요시 편집.
WATCHLIST = ("삼성전자, SK하이닉스, 두산에너빌리티, KB금융, 미래에셋증권, 인탑스, "
             "동진쎄미켐, 한화오션, 미국 CPI/PPI/FOMC, 금통위, "
             "호르무즈/유가/크랙스프레드, 원/달러")

BRIEFING_PROMPT = """너는 한국 개인투자자를 위한 시황 브리핑 어시스턴트다. 아래는 __START__ ~ __END__ (KST) 사이 수집된 뉴스 기사들이다. 텔레그램 메시지 1개(최대 3,000자)로 종합 브리핑을 작성하라. (상단 '오늘·내일 이벤트' 섹션은 코드가 별도로 붙이므로 너는 작성하지 마라.)

[확정 캘린더 — 코드가 확정한 이벤트 시각(KST). 값을 바꾸거나 새로 만들지 마라. 뉴스 항목이 이 이벤트와 관련되면 시각(KST)과 한국 장중/장후 태그를 붙여라]
__CALENDAR_CTX__

원칙:
- 추정 금지. 기사에 없는 내용을 만들지 마라. 수치는 기사 원문의 것만 인용하고, 기사 간 수치가 충돌하면 "출처 간 상이"라고 명시하라.
- 나열이 아니라 종합이다. 같은 사건의 후속·중복 기사는 하나로 묶어라.
- 중요도 순서로 배치하라. 중요도 기준: (1)금리·물가·연준/한은 (2)반도체(삼성전자·SK하이닉스·엔비디아·마이크론) (3)지정학(이란·호르무즈·유가) (4)한국 증시 수급·정책 (5)기타.
- 각 항목에 "그래서 뭐가 중요한가"를 한 줄로 붙여라(사실과 해석을 구분해 해석에는 [해석] 표시).
- 이 투자자의 감시 목록과 교차하는 뉴스는 ★로 강조하라: __WATCHLIST__.
- 시장이 이미 반영했을 가능성(기사 시점 대비 현재)을 주의 문구로 달아라.

형식:
📋 __DATE__ __LABEL__ 다이제스트 (기사 __N__건)
■ 헤드라인 요약 (3줄 이내 — 이 구간의 가장 중요한 흐름)
■ 주요 뉴스 (중요도순, 항목당 2~3줄, ★ 표시 포함)
■ 오늘 확인할 것 (예정된 지표·이벤트가 기사에 있으면 시간과 함께)
■ 낮은 중요도 (제목만 한 줄씩)

반드시 위 형식의 텔레그램 메시지 본문만 출력하라(코드펜스/설명 금지). 3,000자를 넘기지 마라. 각 뉴스 항목엔 이벤트 발생 시각(KST)과 한국 장중/장후 태그를 붙여라.

[기사 목록]
__ARTICLES__
"""


def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line)
    with (LOG_DIR / "digest.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    from secrets_loader import load_config as _load
    return _load()


def collector_status():
    """seen.db runs 테이블의 마지막 상태와 마지막 정상(ok) 시각(KST). 기록 없으면 None."""
    import sqlite3
    try:
        con = sqlite3.connect(ROOT / "seen.db")
        row = con.execute("SELECT status, ts FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return None
        ok = con.execute("SELECT ts FROM runs WHERE status='ok' ORDER BY rowid DESC LIMIT 1").fetchone()
        last_ok = None
        if ok:
            last_ok = datetime.fromisoformat(ok[0]).astimezone(KST).strftime("%m-%d %H:%M")
        return {"status": row[0], "ts": row[1], "last_ok": last_ok}
    except Exception as e:
        log(f"[WARN] collector_status 실패: {e}")
        return None


def parse_articles(start, end, max_excerpt):
    """Read data/news md files overlapping [start,end] (KST) and return
    the articles whose 🕒 timestamp falls in the window, sorted ascending."""
    dates = {start.date(), end.date()}
    arts = []
    for d in dates:
        fpath = DATA_DIR / f"{d:%Y-%m-%d}.md"
        if not fpath.exists():
            continue
        txt = fpath.read_text(encoding="utf-8")
        for b in re.split(r"\n(?=## )", txt):
            mt = re.match(r"## (\d{2}:\d{2}) · (.+)", b)
            if not mt:
                continue
            mts = re.search(r"🕒 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) KST", b)
            if not mts:
                continue
            dt = datetime.strptime(mts.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if not (start <= dt <= end):
                continue
            title = mt.group(2).strip()
            tags_m = re.search(r"🏷 (.+)", b)
            tags = tags_m.group(1).strip() if tags_m else ""
            src_m = re.search(r"📰 ([^·\n]+)", b)
            src = src_m.group(1).strip() if src_m else ""
            # body = lines that aren't meta/link/analysis-quote/separator
            body_lines = []
            for ln in b.split("\n")[1:]:
                s = ln.strip()
                if (not s or s == "---" or s.startswith("- ") or s.startswith("> ")
                        or s.startswith("## ")):
                    continue
                body_lines.append(s)
            body = " ".join(body_lines)
            body = re.sub(r"_\(.*?\)_", "", body).strip()
            excerpt = body[:max_excerpt]
            arts.append((dt, title, tags, src, excerpt))
    arts.sort(key=lambda x: x[0])
    return arts


def build_article_text(arts):
    lines = []
    for dt, title, tags, src, excerpt in arts:
        parts = [f"[{dt:%m-%d %H:%M}] {title}"]
        meta = " ".join([x for x in [src, tags] if x])
        if meta:
            parts.append(f"({meta})")
        if excerpt:
            parts.append(f"— {excerpt}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def run_claude(cfg, prompt):
    dg = cfg.get("digest") or {}
    claude_path = dg.get("claude_path") or "claude"
    timeout = dg.get("timeout_seconds", 300)
    # 장기 토큰(claude setup-token)으로 인증 → 대화형 OAuth 세션 만료 회피.
    env = os.environ.copy()
    tok = (dg.get("claude_oauth_token") or "").strip()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    try:
        # 프롬프트는 stdin으로 전달한다. 명령줄 인자로 넘기면 Windows의
        # 명령줄 길이 한계(~32,767자)를 초과해 큰 구간에서 실패한다.
        proc = subprocess.run(
            [claude_path, "-p"],
            input=prompt,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, cwd=str(ROOT), env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"[ERROR] claude 타임아웃({timeout}s)")
        return None
    if proc.returncode != 0:
        # claude 는 인증 오류 등을 stdout 에 쓰기도 하므로 둘 다 기록한다.
        err = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
        log(f"[ERROR] claude rc={proc.returncode} msg={err[:300]}")
        return None
    return (proc.stdout or "").strip()


def telegram_send(cfg, text):
    tg = cfg.get("telegram") or {}
    token = (tg.get("bot_token") or "").strip()
    chat_id = str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        log("[ERROR] telegram 토큰/chat_id 없음")
        return False
    ok_all = True
    # 텔레그램 4096자 제한 대비 분할(브리핑은 보통 1건).
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for c in chunks:
        payload = json.dumps({
            "chat_id": chat_id, "text": c,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if not json.loads(r.read().decode("utf-8")).get("ok"):
                    ok_all = False
        except Exception as e:
            log(f"[ERROR] telegram 전송 실패: {e}")
            ok_all = False
    return ok_all


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--no-send", action="store_true", help="전송 없이 콘솔 출력만")
    args = ap.parse_args()

    cfg = load_config()
    dg = cfg.get("digest") or {}
    if not dg.get("enabled", True):
        log("digest.enabled=false → 건너뜀")
        return

    now = datetime.now(KST)
    hours = args.hours if args.hours else dg.get("window_hours", 12)
    start = now - timedelta(hours=hours)
    label = "오전" if now.hour < dg.get("morning_before_hour", 14) else "저녁"

    arts = parse_articles(start, now, dg.get("max_excerpt_chars", 220))
    maxn = dg.get("max_articles", 400)
    if len(arts) > maxn:
        arts = arts[-maxn:]
    log(f"다이제스트 준비: {label} | 구간 {start:%m-%d %H:%M}~{now:%m-%d %H:%M} | {len(arts)}건")

    if not arts:
        msg = f"📋 {now:%Y-%m-%d}({'월화수목금토일'[now.weekday()]}) {label} 다이제스트\n해당 구간({start:%m-%d %H:%M}~{now:%H:%M} KST) 신규 기사 없음."
        # 기사 0건은 '진짜 없음'보다 수집기 장애일 때가 훨씬 많다 → seen.db runs 로 원인을 같이 알린다.
        st = collector_status()
        if st and st["status"] != "ok":
            hint = {"session_expired": "수집 소스 인증 만료",
                    "blocked_or_changed": "피드 응답 이상(구조 변경·접속 차단 의심)",
                    "error": "수집기 오류"}.get(st["status"], st["status"])
            msg += (f"\n⚠️ 원인: 수집기 중단 상태 [{st['status']}] — {hint}\n"
                    f"마지막 정상 수집: {st['last_ok'] or '불명'} KST")
        elif st is None:
            msg += "\n⚠️ 수집기 실행 기록 없음(프로세스 미기동 의심) — run_monitor.bat 확인"
        if not args.no_send:
            telegram_send(cfg, msg)
        log(f"구간 내 기사 없음 → 안내 전송 (collector={st['status'] if st else 'none'})")
        return

    # 이벤트 캘린더(evcal): 코드가 확정한 '오늘·내일 이벤트' 섹션 + LLM 컨텍스트
    cal_text, cal_ctx = "", "(캘린더 비활성)"
    try:
        if (cfg.get("calendar") or {}).get("enabled"):
            sys.path.insert(0, str(ROOT))
            from evcal.db import connect as _cal_connect
            from evcal.render import render_calendar_section
            cal_text, cal_ctx = render_calendar_section(_cal_connect(), cfg, now)
    except Exception as e:
        log(f"[WARN] 캘린더 렌더 실패(무시하고 진행): {e}")

    prompt = (BRIEFING_PROMPT
              .replace("__START__", f"{start:%Y-%m-%d %H:%M}")
              .replace("__END__", f"{now:%Y-%m-%d %H:%M}")
              .replace("__WATCHLIST__", WATCHLIST)
              .replace("__DATE__", f"{now:%Y-%m-%d}")
              .replace("__LABEL__", label)
              .replace("__N__", str(len(arts)))
              .replace("__CALENDAR_CTX__", cal_ctx)
              .replace("__ARTICLES__", build_article_text(arts)))

    log(f"claude 호출(프롬프트 {len(prompt)}자)...")
    out = run_claude(cfg, prompt)
    if not out:
        log("[ERROR] 다이제스트 생성 실패 → 전송 안 함")
        # 조용한 실패 방지: 실패 사실을 텔레그램으로 알림.
        if not args.no_send:
            telegram_send(cfg, f"⚠️ 뉴스 다이제스트 생성 실패 ({now:%m-%d %H:%M} {label}).\n"
                               f"claude 호출 오류 — logs/digest.log 확인 필요 "
                               f"(예: 토큰 만료 시 claude setup-token 재발급).")
        return

    # 캘린더 섹션(코드 확정값)을 상단에 결합 → 시각·날짜 환각 방지
    final = (cal_text + "\n\n" + out) if cal_text else out

    # 아카이브
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    (DIGEST_DIR / f"{now:%Y-%m-%d_%H%M}_{label}.md").write_text(final, encoding="utf-8")

    if args.no_send:
        print("\n" + "=" * 60 + "\n" + final + "\n" + "=" * 60)
        log("no-send 모드 → 콘솔 출력만")
        return

    ok = telegram_send(cfg, final)
    log(f"전송 {'성공' if ok else '실패'} ({len(final)}자)")


if __name__ == "__main__":
    main()
