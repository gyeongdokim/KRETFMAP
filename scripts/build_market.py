#!/usr/bin/env python3
"""KRX ETF 일별매매정보를 받아 마켓맵용 data/market.json을 만든다.

사용법
  python scripts/build_market.py                 # 최근 거래일 1일 추가 후 market.json 생성
  python scripts/build_market.py --backfill 60   # 과거 60거래일 히스토리 채우기 (최초 1회)
  python scripts/build_market.py --demo          # 인증키 없이 예시 데이터로 market.json 생성

환경변수
  KRX_AUTH_KEY  KRX Open API 인증키 (ETF 일별매매정보 API 이용신청 필요)
"""
import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "history"
OVERRIDES = DATA / "overrides.csv"
API = "http://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd"  # KRX 데이터는 조회일 기준 전일까지 제공

KEEP_DAYS = 70   # 자금흐름 계산에 쓰는 히스토리 길이 (거래일)
SPARK_DAYS = 20  # 종목 상세 스파크라인 길이
PREM_LIMIT = 30.0  # 괴리율 절댓값(%)이 이보다 크면 이상치로 보고 괴리율을 비운다 (예: 거래가 막힌 러시아 ETF)

# 분류 규칙: 위에서부터 먼저 걸리는 자산군으로 분류. 틀린 종목은 data/overrides.csv로 고친다.
RULES = [
    ("레버리지·인버스", ["레버리지", "인버스", "2X", "곱버스"]),
    ("채권·금리", ["채권", "국고채", "국채", "회사채", "통안", "금리", "CD", "KOFR", "SOFR", "머니마켓", "MMF", "크레딧", "단기채"]),
    ("원자재·통화", ["골드", "금현물", "금선물", "실버", "원유", "WTI", "구리", "농산물", "원자재", "달러", "엔화", "유로선물"]),
    ("리츠·대체", ["리츠", "REIT", "인프라"]),
    ("혼합·자산배분", ["TDF", "TRF", "혼합", "자산배분"]),
    ("해외주식", ["미국", "S&P", "나스닥", "NASDAQ", "다우", "차이나", "중국", "홍콩", "항셍", "일본", "니케이", "TOPIX",
                "인도", "베트남", "대만", "유럽", "유로스탁스", "독일", "글로벌", "선진국", "신흥국", "MSCI WORLD", "ACWI"]),
    ("국내 시장대표", ["200", "코스피", "KOSPI", "코스닥", "KOSDAQ", "KRX300", "KRX 300", "MSCI KOREA", "TOP10"]),
]
DEFAULT_CAT = "국내 섹터·테마"
CATEGORIES = [c for c, _ in RULES] + [DEFAULT_CAT]


def classify(code, name, idx, overrides):
    if code in overrides:
        return overrides[code]
    text = f"{name} {idx}".upper()
    for cat, keywords in RULES:
        if any(k.upper() in text for k in keywords):
            return cat
    return DEFAULT_CAT


def load_overrides():
    out = {}
    if OVERRIDES.exists():
        for line in OVERRIDES.read_text(encoding="utf-8").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] and not parts[0].startswith("#") and parts[1] in CATEGORIES:
                out[parts[0]] = parts[1]
    return out


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------- KRX 호출 ----------

def fetch_day(day, key):
    """해당 일자 ETF 전 종목. 휴장일이면 빈 dict."""
    r = requests.get(API, params={"basDd": day}, headers={"AUTH_KEY": key}, timeout=30)
    if r.status_code == 401:
        sys.exit("인증 실패: KRX_AUTH_KEY 또는 ETF 일별매매정보 API 이용신청을 확인하세요.")
    r.raise_for_status()
    rows = {}
    for x in r.json().get("OutBlock_1", []):
        code = x.get("ISU_SRT_CD") or x.get("ISU_CD")
        if not code:
            continue
        rows[code] = {
            "n": x.get("ISU_NM", ""),
            "i": x.get("IDX_IND_NM", ""),
            "close": num(x.get("TDD_CLSPRC")),
            "nav": num(x.get("NAV")),
            "sh": num(x.get("LIST_SHRS")),
            "aum": num(x.get("INVSTASST_NETASST_TOTAMT")) or num(x.get("MKTCAP")),
            "ret": num(x.get("FLUC_RT")),
            "tv": num(x.get("ACC_TRDVAL")),
        }
    return rows


def save_day(day, rows):
    HIST.mkdir(parents=True, exist_ok=True)
    (HIST / f"{day}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def collect(key, trading_days, max_calendar=None):
    """어제부터 거꾸로 거래일 trading_days개를 채운다. 이미 있는 날짜는 건너뜀."""
    got, d = 0, date.today() - timedelta(days=1)
    limit = max_calendar or trading_days * 2 + 10
    for _ in range(limit):
        if got >= trading_days:
            break
        day = d.strftime("%Y%m%d")
        d -= timedelta(days=1)
        if (HIST / f"{day}.json").exists():
            got += 1
            continue
        if date(int(day[:4]), int(day[4:6]), int(day[6:])).weekday() >= 5:
            continue
        rows = fetch_day(day, key)
        time.sleep(0.2)
        if rows:
            save_day(day, rows)
            got += 1
            print(f"  {day}: {len(rows)}종목")


def load_history():
    files = sorted(HIST.glob("*.json"))[-KEEP_DAYS:]
    return [(f.stem, json.loads(f.read_text(encoding="utf-8"))) for f in files]


def prune_history(keep=KEEP_DAYS + 30):
    files = sorted(HIST.glob("*.json"))
    for f in files[:-keep]:
        f.unlink()


# ---------- 계산 ----------

def flow_of(cur, prev):
    """설정·환매 추정 자금흐름 = 상장좌수 변화 × 당일 NAV."""
    if not prev or cur["sh"] is None or prev["sh"] is None or not cur["nav"]:
        return None
    return (cur["sh"] - prev["sh"]) * cur["nav"]


def build(hist, overrides, demo=False):
    if not hist:
        sys.exit("히스토리가 비어 있습니다. --backfill로 먼저 채우세요.")
    t_day, t_rows = hist[-1]
    p_rows = hist[-2][1] if len(hist) > 1 else {}
    spark_hist = hist[-SPARK_DAYS:]

    cat_of = {}
    etfs = []
    excluded = []  # 괴리율 이상치로 제외한 종목 (화면에서 안내용)
    for code, r in t_rows.items():
        cat = classify(code, r["n"], r["i"], overrides)
        cat_of[code] = cat
        if not r["aum"] or r["aum"] <= 0:
            continue
        prem = (r["close"] - r["nav"]) / r["nav"] * 100 if r["close"] and r["nav"] else None
        if prem is not None and abs(prem) > PREM_LIMIT:
            excluded.append({"c": code, "n": r["n"], "prem": round(prem, 2)})
            prem = None
        flow = flow_of(r, p_rows.get(code))
        sp_prem, sp_sh = [], []
        for _, rows in spark_hist:
            x = rows.get(code)
            if x and x["close"] and x["nav"]:
                p = (x["close"] - x["nav"]) / x["nav"] * 100
                sp_prem.append(round(p, 3) if abs(p) <= PREM_LIMIT else None)
            else:
                sp_prem.append(None)
            sp_sh.append(x["sh"] if x else None)
        etfs.append({
            "c": code, "n": r["n"], "k": cat,
            "aum": round(r["aum"]), "close": r["close"], "nav": r["nav"],
            "ret": None if r["ret"] is None else round(r["ret"], 2),
            "prem": None if prem is None else round(prem, 3),
            "flow": None if flow is None else round(flow),
            "tv": None if r["tv"] is None else round(r["tv"]),
            "sp": {"prem": sp_prem, "sh": sp_sh},
        })

    dates, by_cat = [], {c: [] for c in CATEGORIES}
    for i in range(1, len(hist)):
        day, rows = hist[i]
        prev = hist[i - 1][1]
        acc = defaultdict(float)
        for code, r in rows.items():
            f = flow_of(r, prev.get(code))
            if f is not None:
                cat = cat_of.get(code) or classify(code, r["n"], r["i"], overrides)
                acc[cat] += f
        dates.append(day)
        for c in CATEGORIES:
            by_cat[c].append(round(acc[c]))

    return {
        "asof": t_day,
        "demo": demo,
        "categories": CATEGORIES,
        "spark_dates": [d for d, _ in spark_hist],
        "flows": {"dates": dates, "byCat": by_cat},
        "etfs": sorted(etfs, key=lambda e: -e["aum"]),
        "prem_limit": PREM_LIMIT,
        "excluded": excluded,
    }


# ---------- 예시 데이터 ----------

def demo_history(n_days=45, seed=7):
    """인증키 없이 화면을 확인하기 위한 가상 데이터. 실제 상품과 무관."""
    rng = random.Random(seed)
    brands = ["KODEX", "TIGER", "ACE", "RISE", "SOL", "HANARO", "PLUS"]
    universe = {
        "국내 시장대표": (["200", "코스닥150", "KRX300", "200TR", "코스피"], 9e12, 0.012, 0.0004),
        "국내 섹터·테마": (["반도체", "2차전지산업", "은행", "조선", "방산", "K바이오", "자동차", "AI전력핵심설비"], 1.5e12, 0.02, 0.0006),
        "해외주식": (["미국S&P500", "미국나스닥100", "미국배당다우존스", "차이나전기차", "일본니케이225", "인도니프티50", "미국테크TOP10"], 5e12, 0.013, 0.003),
        "채권·금리": (["CD금리액티브", "KOFR금리액티브", "국고채3년", "종합채권(AA-이상)액티브", "미국채10년선물", "단기채권"], 6e12, 0.002, 0.0003),
        "레버리지·인버스": (["레버리지", "200선물인버스2X", "코스닥150레버리지", "인버스"], 3e12, 0.03, 0.0008),
        "원자재·통화": (["골드선물(H)", "WTI원유선물(H)", "미국달러선물", "구리실물"], 7e11, 0.015, 0.002),
        "리츠·대체": (["리츠부동산인프라", "미국리츠"], 4e11, 0.01, 0.001),
        "혼합·자산배분": (["TDF2045", "TRF3070"], 3e11, 0.008, 0.001),
    }
    BETA = {"채권·금리": 0.03, "원자재·통화": 0.2, "해외주식": 0.4, "리츠·대체": 0.5, "혼합·자산배분": 0.4}
    etfs = []
    k = 0
    for cat, (themes, scale, vol, prem_sd) in universe.items():
        for th in themes:
            for b in rng.sample(brands, rng.randint(1, 4)):
                k += 1
                nav = rng.choice([8000, 10000, 12000, 15000, 25000, 35000, 50000]) * rng.uniform(0.7, 1.4)
                aum = scale / len(themes) * rng.lognormvariate(-0.6, 1.0)
                etfs.append({"c": f"9{k:05d}", "n": f"{b} {th}", "nav": nav, "sh": max(aum / nav, 1e5),
                             "vol": vol, "ps": prem_sd, "drift": rng.gauss(0, 0.004), "beta": BETA.get(cat, 1.0)})
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    days.reverse()
    mkt = 0.0
    hist = []
    for day in days:
        mkt = rng.gauss(0, 0.008)
        rows = {}
        for e in etfs:
            beta = 1.8 if "레버리지" in e["n"] else -1.0 if "인버스" in e["n"] else e["beta"]
            ret = mkt * beta + rng.gauss(0, e["vol"])
            e["nav"] *= 1 + ret
            if rng.random() < 0.35:
                e["sh"] = max(round(e["sh"] * (1 + e["drift"] + rng.gauss(0, 0.01)), -4), 1e5)
            prem = rng.gauss(0, e["ps"]) + (rng.gauss(0, 0.01) if rng.random() < 0.01 else 0)
            close = round(e["nav"] * (1 + prem) / 5) * 5
            rows[e["c"]] = {"n": e["n"], "i": "", "close": close, "nav": round(e["nav"], 2), "sh": e["sh"],
                            "aum": e["nav"] * e["sh"], "ret": ret * 100, "tv": e["nav"] * e["sh"] * rng.uniform(0.002, 0.05)}
        hist.append((day, rows))
    return hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="과거 N거래일 채우기")
    ap.add_argument("--demo", action="store_true", help="예시 데이터로 생성")
    args = ap.parse_args()
    overrides = load_overrides()

    if args.demo:
        hist = demo_history()
        market = build(hist, overrides, demo=True)
    else:
        key = os.environ.get("KRX_AUTH_KEY")
        if not key:
            sys.exit("KRX_AUTH_KEY 환경변수가 없습니다.")
        print("KRX 데이터 수집")
        collect(key, max(args.backfill, 2))
        prune_history()
        market = build(load_history(), overrides)

    DATA.mkdir(exist_ok=True)
    out = DATA / "market.json"
    out.write_text(json.dumps(market, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"완료: {out} ({market['asof']}, {len(market['etfs'])}종목, 자금흐름 {len(market['flows']['dates'])}일)")
    for e in market["excluded"]:
        print(f"  괴리율 이상치 제외: {e['n']} ({e['prem']:+.2f}%)")


if __name__ == "__main__":
    main()
