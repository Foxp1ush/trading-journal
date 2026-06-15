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
    import prices  # noqa: F401
    import sheets  # noqa: F401
    check("import journal_core / prices / sheets", True)
except Exception as exc:  # noqa: BLE001
    check("import journal_core / prices / sheets", False, repr(exc))
    print("\n임포트 실패 — 이후 검사 생략.")
    sys.exit(1)

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

# 3) 앱 부팅 (AppTest) — exception 0
try:
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("journal_app.py", default_timeout=60).run()
    check("journal_app.py 부팅 exception 0", len(at.exception) == 0,
          "; ".join(str(e.value) for e in at.exception))
except Exception as exc:  # noqa: BLE001
    check("journal_app.py 부팅 exception 0", False, repr(exc))

# 결과
print()
if failures:
    print(f"❌ 실패 {len(failures)}건: {', '.join(failures)}")
    print("→ push 하지 마세요. 위 항목을 고친 뒤 다시 실행.")
    sys.exit(1)
print("✅ 모든 점검 통과 — push 해도 안전.")
sys.exit(0)
