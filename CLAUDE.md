# 한국 ETF 마켓맵 (kr-etf-monitor)

정적 사이트 + GitHub Actions 일 배치. 서버 없음.

## 구조
- `index.html`: 마켓맵 화면 전체 (D3 트리맵, CSS/JS 인라인). `data/market.json`만 읽는다.
- `scripts/build_market.py`: KRX Open API `etp/etf_bydd_trd` → `data/history/YYYYMMDD.json` 누적 → `data/market.json` 생성.
- `data/overrides.csv`: 자산군 자동 분류 수동 보정.
- `.github/workflows/daily.yml`: 평일 07:30 KST 실행, 데이터 커밋.

## 지표 정의 (바꾸면 index.html 하단 설명도 같이 수정)
- 자금흐름 = (당일 상장좌수 − 전일 상장좌수) × 당일 NAV
- 괴리율 = (종가 − NAV) ÷ NAV × 100
- 타일 크기 = 순자산총액 (INVSTASST_NETASST_TOTAMT, 없으면 시가총액)

## 명령
- 로컬 확인: `python scripts/build_market.py --demo` 후 `python -m http.server`
- 실데이터: `KRX_AUTH_KEY=... python scripts/build_market.py --backfill 60`

## 규칙
- 화면 문구는 한국어. 색은 한국 시장 관례대로 상승·유입 빨강, 하락·유출 파랑.
- `data/market.json` 스키마를 바꾸면 index.html의 읽는 부분도 함께 수정.
