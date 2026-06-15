"""push 전 자동 점검 게이트 — 앱이 깨졌는지 수 초 안에 확인.

실행: .venv\\Scripts\\python.exe smoke_test.py   (또는 check.ps1)
통과하면 exit 0, 실패하면 exit 1 (= push 하지 말 것).

검사 항목 (네트워크 불필요):
 1) 핵심 모듈 import — 문법/임포트 오류
 2) journal_core 평균단가 검산 — 로직 회귀 방지
 3) journal_app 부팅(AppTest) — 런타임 오류/메인 파일 실수 탐지
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("=== 매매일지 smoke test ===")

# 1) import
try:
    import journal_core as jc
    import krx
    import prices  # noqa: F401
    import sheets  # noqa: F401
    check("import journal_core / krx / prices / sheets", True)
except Exception as exc:  # noqa: BLE001
    check("import journal_core / krx / prices / sheets", False, repr(exc))
    print("\n임포트 실패 — 이후 검사 생략.")
    sys.exit(1)

# 국내 티커 변환 (네트워크 불필요)
check("to_yf_ticker KOSPI→.KS", krx.to_yf_ticker("005930", "KOSPI") == "005930.KS")
check("to_yf_ticker KOSDAQ→.KQ", krx.to_yf_ticker("277810", "KOSDAQ") == "277810.KQ")

# 2) 코어 검산 (가격 None = 거래 이벤트 점만)
sample = [
    {"date": "2026-06-01", "time": "09:30", "ticker": "AAPL", "side": "BUY",  "price": 150.0, "shares": 100},
    {"date": "2026-06-01", "time": "10:00", "ticker": "AAPL", "side": "BUY",  "price": 140.0, "shares": 50},
    {"date": "2026-06-01", "time": "14:00", "ticker": "AAPL", "side": "SELL", "price": 160.0, "shares": 80},
    {"date": "2026-06-02", "time": "09:31", "ticker": "NVDA", "side": "BUY",  "price": 100.0, "shares": 10},
    {"date": "2026-06-03", "time": "15:00", "ticker": "NVDA", "side": "SELL", "price": 120.0, "shares": 10},
]
res = jc.process_portfolio(sample)
aapl, nvda = res.get("AAPL"), res.get("NVDA")

check("종목 2개 집계됨", set(res) == {"AAPL", "NVDA"}, f"got {set(res)}")
check("AAPL 평단 ≈146.6667", aapl and abs(aapl.avg_cost - 146.6667) < 1e-3,
      f"{aapl.avg_cost if aapl else None}")
check("AAPL 보유 70주", aapl and abs(aapl.position_shares - 70) < 1e-9,
      f"{aapl.position_shares if aapl else None}")
check("AAPL 실현손익 ≈1066.67", aapl and abs(aapl.realized_pnl - 1066.6667) < 1e-2,
      f"{aapl.realized_pnl if aapl else None}")
check("NVDA 실현손익 =200, 보유 0",
      nvda and abs(nvda.realized_pnl - 200) < 1e-9 and nvda.position_shares == 0,
      f"{(nvda.realized_pnl, nvda.position_shares) if nvda else None}")

# 같은 시각 BUY/SELL — 매수 먼저 처리되어 보유 0 (입력순이 SELL 먼저여도)
same_ts = jc.process_portfolio([
    {"date": "2026-05-20", "time": "09:30", "ticker": "GCTS", "side": "SELL", "price": 2.5, "shares": 100},
    {"date": "2026-05-20", "time": "09:30", "ticker": "GCTS", "side": "BUY",  "price": 2.1, "shares": 100},
])["GCTS"]
check("동일시각 매수→매도, 보유 0 & 실현 +40",
      abs(same_ts.position_shares) < 1e-9 and abs(same_ts.realized_pnl - 40) < 1e-6,
      f"보유={same_ts.position_shares}, 실현={same_ts.realized_pnl}")

# 초과매도 경고 동작
over = jc.process_transactions(
    [{"date": "2026-06-01", "time": "09:30", "ticker": "X", "side": "SELL", "price": 10, "shares": 5}],
    ticker="X",
)
check("초과매도 경고 발생", len(over.warnings) >= 1)

# 현금/계좌가치 (입금 20000 → BUY 100@150 → SELL 80@160 → cash 17800, 보유 20)
cash_txns = [
    {"date": "2026-06-01", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 20000, "shares": 0},
    {"date": "2026-06-01", "time": "09:30", "ticker": "AAPL", "side": "BUY",  "price": 150, "shares": 100},
    {"date": "2026-06-01", "time": "14:00", "ticker": "AAPL", "side": "SELL", "price": 160, "shares": 80},
]
acc = jc.current_account(cash_txns, {"AAPL": 165.0})
check("현금잔고 =17800", abs(acc["cash"] - 17800) < 1e-6, f"{acc['cash']}")
check("보유시가 =3300 (20×165)", abs(acc["holdings_value"] - 3300) < 1e-6, f"{acc['holdings_value']}")
check("계좌가치 =21100", abs(acc["account_value"] - 21100) < 1e-6, f"{acc['account_value']}")
check("자산배분에 현금 포함", "현금" in acc["allocation"] and "AAPL" in acc["allocation"])

# 계좌가치 곡선 — 합성 가격으로 정상 동작
import pandas as _pd
_aapl = _pd.DataFrame(
    {"Close": [155.0, 158.0]},
    index=_pd.to_datetime(["2026-06-01", "2026-06-02"]),
)
avc = jc.account_value_curve(cash_txns, {"AAPL": _aapl})
check("계좌가치 곡선 생성", not avc.empty and "계좌가치" in avc.columns,
      f"empty={avc.empty}")

# 다중통화 — USD/KRW 분리, 통화별 독립 계좌
check("통화 판정 .KS→KRW, 일반→USD",
      jc.currency_of_ticker("005930.KS") == "KRW" and jc.currency_of_ticker("AAPL") == "USD")
multi = [
    {"date": "2026-06-01", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 2000, "shares": 0, "currency": "USD"},
    {"date": "2026-06-01", "time": "09:30", "ticker": "AAPL", "side": "BUY", "price": 150, "shares": 10, "currency": "USD"},
    {"date": "2026-06-02", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 1000000, "shares": 0, "currency": "KRW"},
    {"date": "2026-06-02", "time": "09:30", "ticker": "005930.KS", "side": "BUY", "price": 70000, "shares": 10, "currency": "KRW"},
]
buckets = jc.split_by_currency(multi)
check("통화 2버킷(USD/KRW)", set(buckets) == {"USD", "KRW"}, f"{set(buckets)}")
usd_acc = jc.current_account(buckets["USD"], {"AAPL": 160.0})
krw_acc = jc.current_account(buckets["KRW"], {"005930.KS": 72000.0})
check("USD 현금 =500 (2000−1500)", abs(usd_acc["cash"] - 500) < 1e-6, f"{usd_acc['cash']}")
check("USD 계좌가치 =2100 (500+10×160)", abs(usd_acc["account_value"] - 2100) < 1e-6, f"{usd_acc['account_value']}")
check("KRW 현금 =300000 (100만−70만)", abs(krw_acc["cash"] - 300000) < 1e-6, f"{krw_acc['cash']}")
check("KRW 계좌가치 =1,020,000 (30만+10×72000)", abs(krw_acc["account_value"] - 1020000) < 1e-6, f"{krw_acc['account_value']}")

# journal_app 표시 헬퍼 회귀 (네트워크 불필요)
import journal_app as app  # noqa: E402 — 코어 검산 뒤 임포트

# loaded_df 모양(가격·수량 numeric, 빈칸은 NaN) — df_to_txns의 실제 입력과 동일
_df = _pd.DataFrame([
    {"date": "2026-06-01", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 1000.0, "shares": None, "currency": "USD"},
    {"date": "2026-06-01", "time": "09:30", "ticker": "aapl", "side": "BUY", "price": 150.0, "shares": 10.0, "currency": "USD"},
    {"date": "2026-06-02", "time": "", "ticker": "MSFT", "side": "BUY", "price": None, "shares": 5.0, "currency": "USD"},  # 가격 없음 → 스킵
])
_tx = app.df_to_txns(_df)
check("df_to_txns 현금+매매 2건(빈 가격행 스킵)", len(_tx) == 2, f"{len(_tx)}")
check("df_to_txns 티커 대문자 정규화", _tx[1]["ticker"] == "AAPL", f"{_tx[1]['ticker']}")
check("df_to_txns 현금행 ticker 빈문자", _tx[0]["ticker"] == "" and _tx[0]["side"] == "DEPOSIT")

_mres = jc.process_portfolio([
    {"date": "2026-06-01", "time": "09:30", "ticker": "X", "side": "BUY", "price": 10, "shares": 10},
    {"date": "2026-06-02", "time": "09:30", "ticker": "X", "side": "SELL", "price": 12, "shares": 4},
])["X"]
_mk = app._trade_markers(_mres.rows)
check("_trade_markers 매수·매도 2개", len(_mk) == 2 and set(_mk["거래"]) == {"매수", "매도"},
      f"{None if _mk.empty else list(_mk['거래'])}")
check("_trade_markers y=실현+(가격−평단)×보유 (매수점=0)",
      abs(float(_mk.iloc[0]["값"])) < 1e-9, f"{None if _mk.empty else _mk.iloc[0]['값']}")

# build_manual_row (빠른 입력 검증)
_row, _err = app.build_manual_row("2026-06-01", "09:30", "aapl", "BUY", 150.0, 10.0, "USD", "계획", "분할매수")
check("build_manual_row 매매 정상(티커 대문자·수량·태그·메모)",
      _err == "" and _row and _row["ticker"] == "AAPL" and _row["shares"] == 10.0
      and _row["tag"] == "계획" and _row["note"] == "분할매수" and set(_row) == set(sheets.HEADER),
      f"{_err}")
_crow, _cerr = app.build_manual_row("2026-06-01", "", "", "DEPOSIT", 1000.0, None, "KRW")
check("build_manual_row 현금: 티커 무시·shares None",
      _cerr == "" and _crow and _crow["ticker"] == "" and _crow["shares"] is None, f"{_cerr}")
_n1, _e1 = app.build_manual_row("2026-06-01", "09:30", "AAPL", "BUY", None, 10.0, "USD")
_n2, _e2 = app.build_manual_row("2026-06-01", "09:30", "", "BUY", 150.0, 10.0, "USD")
_n3, _e3 = app.build_manual_row("2026-06-01", "09:30", "AAPL", "BUY", 150.0, None, "USD")
check("build_manual_row 누락 시 (None, 에러)",
      _n1 is None and _e1 and _n2 is None and _e2 and _n3 is None and _e3)

_seed = app.prepare_edit_df(None)
check("prepare_edit_df 빈 입력 시 예시 2행 시드", len(_seed) == 2, f"{len(_seed)}")
check("prepare_edit_df date=datetime · price=numeric dtype",
      str(_seed["date"].dtype).startswith("datetime") and "float" in str(_seed["price"].dtype),
      f"{_seed['date'].dtype}, {_seed['price'].dtype}")

# 태그별 매매 피드백 — 계획 매도 이익(+), 충동 매도 손실(−)
_tagres = jc.process_portfolio([
    {"date": "2026-06-01", "time": "09:30", "ticker": "T", "side": "BUY", "price": 100, "shares": 10, "tag": "계획"},
    {"date": "2026-06-02", "time": "09:30", "ticker": "T", "side": "SELL", "price": 120, "shares": 10, "tag": "계획"},   # +200
    {"date": "2026-06-03", "time": "09:30", "ticker": "U", "side": "BUY", "price": 50, "shares": 10, "tag": "충동"},
    {"date": "2026-06-04", "time": "09:30", "ticker": "U", "side": "SELL", "price": 40, "shares": 10, "tag": "충동"},    # −100
])
_perf = jc.tag_performance(_tagres)
_byt = {r["태그"]: r for r in _perf.to_dict("records")}
check("tag_performance 계획 실현 +200·승률100",
      abs(_byt["계획"]["실현손익"] - 200) < 1e-6 and _byt["계획"]["승률%"] == 100.0, f"{_byt.get('계획')}")
check("tag_performance 충동 실현 −100·승률0",
      abs(_byt["충동"]["실현손익"] + 100) < 1e-6 and _byt["충동"]["승률%"] == 0.0, f"{_byt.get('충동')}")
check("tag_performance 매매횟수=매수+매도(계획 2)", _byt["계획"]["매매횟수"] == 2, f"{_byt['계획']['매매횟수']}")
app._tag_bar(_perf, "USD").to_dict()  # Altair 스펙 유효성(예외 없으면 통과)
check("_tag_bar Altair 스펙 빌드 OK", True)

# 3) 앱 부팅 (AppTest) — exception 0
try:
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("journal_app.py", default_timeout=60).run()
    check("journal_app.py 부팅 exception 0", len(at.exception) == 0,
          "; ".join(str(e.value) for e in at.exception))
except Exception as exc:  # noqa: BLE001
    check("journal_app.py 부팅 exception 0", False, repr(exc))

# 연결 상태 렌더 — 게이트 우회로 탭·빠른입력 폼·전체표가 예외 없이 그려지는지(네트워크 불필요)
try:
    from streamlit.testing.v1 import AppTest
    _cash_only = _pd.DataFrame([
        {"date": "2026-06-01", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 1000.0, "shares": None, "currency": "USD", "tag": "", "note": "월급"},
    ])
    at3 = AppTest.from_file("journal_app.py", default_timeout=60)
    at3.session_state["ws_ok"] = True
    at3.session_state["sheet_url"] = "https://example.com/x"
    at3.session_state["loaded_df"] = _cash_only
    at3.session_state["draft_df"] = _cash_only.copy()
    at3.run()
    check("연결 상태 렌더(탭·빠른입력·전체표) exception 0", len(at3.exception) == 0,
          "; ".join(str(e.value) for e in at3.exception))
except Exception as exc:  # noqa: BLE001
    check("연결 상태 렌더(탭·빠른입력·전체표) exception 0", False, repr(exc))

# 결과
print()
if failures:
    print(f"❌ 실패 {len(failures)}건: {', '.join(failures)}")
    print("→ push 하지 마세요. 위 항목을 고친 뒤 다시 실행.")
    sys.exit(1)
print("✅ 모든 점검 통과 — push 해도 안전.")
sys.exit(0)
