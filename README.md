# news-monitor

개인 투자용 **뉴스·일정 모니터**입니다. 공개 RSS와 공식 공시 API를 모아 하루 두 번 시황 브리핑을 만들고, 텔레그램으로 받습니다. 파이썬 표준 라이브러리만으로 동작합니다(`tzdata` 제외).

## 구성

| 모듈 | 하는 일 |
|---|---|
| `rss_collector.py` | 등록된 RSS/Atom 피드를 주기적으로 읽어 새 항목만 `data/news/날짜.md`에 기록. 중복 제거는 SQLite |
| `digest.py` | 직전 12시간 수집분을 모아 `claude -p`로 한국어 브리핑을 만들고 텔레그램 1건으로 전송 |
| `evcal/` | 이벤트 캘린더 — 미국 경제지표(BLS·연준 일정), SEC EDGAR 8-K, OpenDART 공시, 휴장일, 수동 일정을 모아 “오늘·내일 이벤트”를 코드로 확정해 브리핑 상단에 붙임 |
| `btc_nav.py` | 비트코인 보유 기업(MSTR 등)의 SEC 8-K를 파싱해 보유량·주식수로 mNAV를 계산하고 이상 징후 알림 |
| `secrets_loader.py` | 설정과 비밀값 분리 — 토큰은 `secrets/` 또는 환경변수에서만 읽음 |

```
RSS 피드 ──► rss_collector ──► data/news/*.md ──┐
SEC·DART·BLS·연준 ──► evcal (events.db) ─────────┼──► digest ──► claude -p ──► 텔레그램
SEC 8-K ──► btc_nav ─────────────────────────────┘ (별도 알림)
```

## 데이터 소스 원칙

- **공개적으로 제공되는 피드와 공식 API만** 사용합니다. 기본 RSS는 연방준비제도·SEC·한국은행 보도자료입니다.
- 기사 원문 페이지를 긁지 않습니다. 피드에 들어 있는 제목·요약·링크만 저장합니다.
- SEC EDGAR는 [공정접근 정책](https://www.sec.gov/os/accessing-edgar-data)에 따라 연락처가 담긴 User-Agent를 보내고 요청 간격을 둡니다.
- `btc_prices.py`의 BTC 시세는 Coinbase·CoinGecko·Kraken 공개 API, 주가(MSTR 등)는 Yahoo Finance의 **비공식** 차트 엔드포인트를 씁니다. 개인 연구용이며 Yahoo 이용약관이 적용됩니다.
- 새 소스를 추가할 때는 robots.txt와 이용약관을 먼저 확인합니다(`docs/calendar_design.md` 3장).

## 설치와 실행

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json
```

비밀값은 `secrets/` 폴더에 파일로 둡니다(자세한 목록은 `secrets/README.md`). 텔레그램 없이도 수집과 브리핑 생성은 동작합니다.

```bash
python rss_collector.py --once --no-send   # RSS 1회 수집
python digest.py --no-send                 # 브리핑을 콘솔로만 출력
python -m evcal.refresh --show             # 오늘·내일 이벤트
python btc_nav.py --no-send                # mNAV 계산
```

상시 수집은 `run_monitor.bat`, 브리핑·캘린더는 Windows 작업 스케줄러에 `digest.py`(08:00·20:00), `python -m evcal.refresh`(06:00·17:00)를 등록해 씁니다.

## 설계에서 신경 쓴 점

- **추정하지 않기** — 공시 파싱에 실패하면 값을 채우지 않고 `미검증`으로 표시합니다.
- **캘린더는 코드가 확정** — 이벤트 시각은 LLM이 아니라 `evcal`이 정하고, LLM은 그 목록을 바꾸지 못하게 프롬프트로 고정합니다(`CLAUDE.md`).
- **장애를 숨기지 않기** — 수집 구간에 기사가 0건이면 “뉴스 없음”이 아니라 수집기 상태(`runs` 테이블)를 함께 보냅니다.
