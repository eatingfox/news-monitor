# 이벤트 캘린더 모듈 — 설계안 (뉴스 모니터 확장)

상태: **Phase 1 구현 완료·라이브** (2026-09-05). 패키지명은 stdlib `calendar` 충돌 회피로 `evcal/`.
- 스케줄: `NewsMonitorCalendarRefresh` 매일 06:00·17:00 → events.db / 디제스트(08:00·20:00)가 상단 섹션 주입
- 확인된 한계: **BLS 일정 페이지가 403(봇 차단)** → NFP/CPI/PPI는 규칙 기반 **"(예상일)"** 표기로 폴백(추정을 확정처럼 보이지 않게). Phase 2에서 BLS API(무료 등록키) 또는 허용 대체 소스로 확정값 확보 예정.
- DART: 키 파일 연동 완료(2026-09-08). Phase 3: 사후 검증 루프(verify.py).
- **Phase 2-1 (2026-09-13) 8-K 본문 파싱 라이브** — `evcal/sources/edgar_items.py`. 최근 45일 8-K(Item 2.02/7.01/8.01)의 본문+EX-99 보도자료에서
  "will host Investor Day on …", "will participate in the X Conference on …", "will report results on …" 문장의 **미래 날짜**를 추출
  → `src=edgar8k` 이벤트(인베스터데이·컨퍼런스 참석·실적일·주주총회·제품행사). 접수번호별 결과는 `events.db.edgar_parsed`에 캐시.
  오탐 방지: 회계기간 표기("quarter ending Sept 30") 제외, 미래형 표현(will/plans to/scheduled…) 필수, forward-looking 보일러플레이트 제외.
  **한계**: 회사가 8-K로 내지 않는 예고(대부분의 실적일 공지·컨퍼런스 참석 보도자료, 예: 릴리)는 못 잡음 → Phase 2-2(IR 페이지) 또는 manual.json.
- 렌더: 일요일 저녁·월요일 오전 다이제스트에 "▶ 이번 주 ★" 7일 전망 줄 추가. manual.json `confirmed:false`는 [예상일] 태그로 표시.
작성: 2026-09-05 / 갱신 2026-09-13

---

## 0. 진단 검증 — 현 시스템(collector.py / digest.py) 대비

| 진단 항목 | 검증 결과 |
|---|---|
| ① "일어난 일"만 긁고 "일어날 일"을 안 봄 (캘린더 레이어 부재) | **맞음. 핵심 원인.** 디제스트의 "■ 오늘 확인할 것"은 *기사에 언급된* 예정 이벤트만 실어서, 사전 기사가 없는 이벤트(샌디스크 인베스터데이)는 존재 자체를 모름. |
| ② "기사 수 = 중요도" 가중치 | **부분 보정 필요.** 현 봇은 기사 수로 매기지 않고 LLM이 사용자 지정 순위(금리·물가 > 반도체 > 지정학 > 한국 > 기타)로 배치함. 다만 **정량 점수(시총×불확실성×임박성)는 없고**, 무엇보다 **못 본 것은 순위를 매길 수 없음** → 결국 ①이 본질. 점수는 ①을 보강하는 개선. |
| ③ 이벤트 발생 시각(ET→KST)·장중/장후 개념 부재 | **맞음.** 기사 발행 시각만 있고 이벤트 시각 개념 없음. |

→ 결론: **캘린더 파이프라인 신설 + 디제스트 시점에 조인**. 뉴스 수집(collector.py)은 건드리지 않음.

---

## 1. 아키텍처 (기존과 분리)

```
collector.py (불변)  ── 5분 폴링 → seen.db, data/news/*.md
calendar/
  ├─ refresh.py      ── 매일 06:00: 소스 수집 → events.db  (Phase 1)
  ├─ render.py       ── 디제스트용 "■ 오늘·내일 이벤트(KST)" 섹션을 **코드로** 렌더
  ├─ score.py        ── 중요도 = 시총 × 불확실성 × 임박성
  ├─ verify.py       ── 사후 검증 루프 (Phase 3)
  └─ sources/        ── 소스별 어댑터 (각각 robots/TOS 확인 기록 포함)
events.db (SQLite)
digest.py (수정)     ── events.db 읽어 프롬프트 상단에 캘린더 섹션 주입 + 뉴스와 조인
```

원칙:
- **날짜·시각·컨센서스 수치는 코드가 확정해 넣는다.** LLM에게 캘린더를 "생성"시키지 않음(환각 방지). LLM은 뉴스 종합과 이벤트-뉴스 연결만 담당.
- collector.py는 손대지 않음(안정성 유지).

---

## 2. events.db 스키마

```sql
CREATE TABLE events (
  id TEXT PRIMARY KEY,            -- src:외부id 또는 해시
  name TEXT, category TEXT,       -- macro | earnings | corp | conf | kr_dart | policy
  tickers TEXT,                   -- JSON 배열 (관련 종목/섹터)
  when_et TEXT, when_kst TEXT,    -- ISO, DST 반영(zoneinfo America/New_York)
  us_session TEXT, kr_session TEXT,  -- 장전/장중/장후 (각 시장 기준)
  consensus TEXT, impact TEXT,    -- 컨센(있으면), 영향 태그(금리·환율·반도체…)
  mcap REAL, uncertainty REAL, importance REAL,
  is_watchlist INTEGER,           -- 관심종목이면 1 → 상단 고정
  url TEXT, src TEXT,
  first_seen TEXT, last_seen TEXT, status TEXT   -- upcoming | done | cancelled
);
CREATE TABLE calendar_gaps (       -- Phase 3 사후검증 결과
  date TEXT, ticker TEXT, move_pct REAL, had_event INTEGER, note TEXT
);
```

---

## 3. 데이터 소스 — **소스별 robots/TOS 확인이 채택 조건** (무단 수집 금지 원칙)

### Phase 1 (공식·공개 → 채택 가능성 높음)
| 소스 | 내용 | TOS 상태(확인 예정) |
|---|---|---|
| BLS / BEA 공식 발표 일정 | NFP·CPI·PPI·GDP·PCE 일정 | 공개 정부 데이터 (양호) |
| 연준 FOMC 일정 (federalreserve.gov) | 회의·의사록·기자회견 | 공개 (양호) |
| BOJ / ECB 일정 | 회의일 | 공개 (양호) |
| 한국은행 금통위 일정 | 회의일 | 공개 (양호) |
| DART OpenAPI (공식, 무료 키) | 관심 KR 종목 공시·예정(재공시 예정 포함) | 공식 API (양호, 키 발급 필요) |
| SEC EDGAR 공식 피드 (8-K 등) | 관심 US 종목 Item 2.02/7.01 등 | 공식, fair-use User-Agent 필요 (양호) |
| 연간 컨퍼런스 마스터 목록 | 세미콘·CES·GTC·잭슨홀·블랙햇… 20~30개 | 수동 작성(제가 초안 → 검수) |

### Phase 2 (사이트별 확인 필수)
| 소스 | 내용 | 주의 |
|---|---|---|
| 관심종목 IR 캘린더 | 인베스터데이·애널리스트데이·제품발표·주총 | **RSS/ICS 우선**, 스크래핑은 robots/TOS 확인 후. 사이트별 상이 |
| 실적 발표일 | S&P500·나스닥100·코스피200 | 무료 소스 제한적 → 허용되는 소스만. 없으면 관심종목 IR로 한정 |

### Phase 3
- 사후 검증 루프(§6).

**채택 절차:** 소스 추가 시 `sources/<name>.py` 상단에 robots.txt·TOS 확인 결과·날짜를 주석으로 기록. 금지/불명확이면 제외하고 리포트.

---

## 4. 중요도 점수 = 시총 × 불확실성 × 임박성

- **시총**: yfinance/FDR (스캐너 프로젝트 유틸 재사용). 섹터 대표 이벤트면 섹터 시총 근사. 매크로(NFP·FOMC)는 최상위 고정 가중.
- **불확실성 계수** (사전 정보 부족 = 높음, 수동 표):
  - 인베스터데이·제품발표·정책결정(FOMC 등) **1.0**
  - 실적 발표 **0.6**
  - 정기 지표(CPI·NFP 등, 컨센 존재) **0.5**
  - 산업 컨퍼런스 **0.4**
- **임박성**: D-day **1.0** / D-1 **0.8** / D-2~3 **0.5** / 그 외 **0.2**
- **관심종목 이벤트는 점수 무관 상단 고정.**
- 점수는 "낮은 중요도" 섹션에도 노출(분류 근거 공개).

---

## 5. 디제스트 출력 변경 (digest.py)

1. **맨 위 신설: `■ 오늘·내일 이벤트 (KST)`** — 시각순, 코드 렌더
   ```
   21:30 KST | 美 8월 NFP | 컨센 +5.6만 | 장전 | 영향: 금리·환율·반도체
   ```
2. 뉴스 항목마다 **이벤트 발생 시각(KST)** + **한국 장중/장후 태그** 필수 (프롬프트 규칙 추가, 시각은 코드가 제공)
3. "낮은 중요도"에 점수 표기
4. **D-3부터 매일 리마인드, D-day 시각 강조**
5. 기존 브리핑 프롬프트·감시목록은 유지, 캘린더 섹션만 위에 얹음

---

## 6. 사후 검증 루프 (Phase 3 — 장기적으로 가장 가치 큼)

- 매일 **US 마감 후(07:00 KST)** / **KR 마감 후(16:00 KST)**:
  1. 관심종목 + 대형주 중 **당일 |등락| ≥ 5%** 추출 (yfinance/FDR)
  2. events.db에 **당일 해당 종목 이벤트가 있었는지** 조회
  3. 없으면 → `calendar_gaps`에 기록 + `logs/calendar_gaps.md` + 텔레그램 한 줄 요약
     ("SNDK +12%, 캘린더 미등록 → 원인 확인·소스 보강 필요")
- 이 루프가 "왜 못 잡았지"를 시스템이 스스로 찾아 소스를 보강하는 구조. (샌디스크 인베스터데이가 여기서 걸렸을 것)

---

## 7. 스케줄 (기존 패턴 재사용)

| 작업 | 시각 | 방식 |
|---|---|---|
| calendar refresh | 매일 06:00 | Task Scheduler (`pythonw calendar/refresh.py`) |
| 디제스트 | 08:00 / 20:00 (기존) | events.db 읽음 |
| 사후검증 | 07:00 / 16:00 | Task Scheduler (Phase 3) |

---

## 8. 리스크·한계 (정직)

- **소스 TOS**: 개별 확인 없이는 채택 안 함. IR 스크래핑·실적발표일은 무료 소스 한계 있음 → Phase 2에서 허용 범위만.
- **컨센서스 수치**: 무료로 안정적인 소스가 제한적 → 없으면 "컨센 미확인"으로 표기(추정 금지).
- **환각 방지**: 시각·날짜·컨센은 코드 확정값만 프롬프트에 주입.
- **관심종목(US) 목록** 필요: KR은 디제스트 감시목록에 있으나 US 개별 종목(샌디스크·테슬라 등)은 확정 필요.

---

## 9. 결정 요청 (구현 전)

1. **Phase 1 범위 승인** — 공식 매크로 캘린더 + 컨퍼런스 마스터 + DART API + SEC EDGAR(관심종목) + 디제스트 캘린더 섹션
2. **US 관심종목 목록** (IR·8-K 추적용) — 예: SNDK, MU, NVDA, TSLA, AVGO…
3. **refresh 시각** 06:00 OK?
4. **착수 승인** → Phase 1 구현 후 며칠 검증 → Phase 2·3

---

## 부록 A. 연간 컨퍼런스 마스터 초안 (검수용, 20~30개)
(구현 시 별도 `sources/conferences.yaml`로 작성 — 반도체·AI·바이오·매크로 고정 이벤트. 날짜는 연도별 확인 필요)
- 반도체/AI: CES(1월), GTC(3월), 세미콘 코리아/차이나/웨스트, 컴퓨텍스(5~6월), 핫칩스(8월), 플래시 메모리 서밋(8월), IEDM(12월), 애플 WWDC(6월)/9월 이벤트, 삼성 언팩·파운드리 포럼, TSMC 테크 심포지엄, 마이크로소프트 빌드, 구글 I/O, AWS 리인벤트(11~12월)
- 매크로: 잭슨홀(8월), IMF/WB 총회, 다보스(1월), FOMC(연 8회)
- 바이오: JPM 헬스케어(1월), ASCO(6월), AACR
- 보안: 블랙햇/데프콘(8월), RSA(4~5월)
