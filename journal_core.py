"""매매일지 코어 — 여러 종목 평균단가 엔진 + 하이브리드 누적손익 곡선.

stock-trending-bot의 src/sim/journal.py(단일 종목)를 포팅하고, core 의존을 제거해
가격 프레임을 인자로 주입받도록 했다. 여기에 '여러 종목' 집계(process_portfolio)와
포트 전체 누적손익 곡선(portfolio_pnl_curve)을 추가한다.

원가는 평균단가(평단) 방식 — 국내 증권사 표시와 동일.
순수 로직(Streamlit·네트워크 비의존). 가격은 호출자가 prices.py로 받아 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

import pandas as pd

BUY = "BUY"
SELL = "SELL"


class Txn(TypedDict):
    """거래 한 건. price/shares는 양수, side는 'BUY'|'SELL'."""
    date: str    # 'YYYY-MM-DD'
    time: str    # 'HH:MM'
    ticker: str
    side: str
    price: float
    shares: float


@dataclass
class ProcessedRow:
    """거래 처리 후 스냅샷 — 감사 추적 한 줄."""
    ts: pd.Timestamp
    side: str
    price: float
    shares: float           # 실체결 수량(매도는 초과분 제외)
    position_shares: float
    avg_cost: float
    realized_pnl: float
    note: str = ""


@dataclass
class JournalResult:
    ticker: str
    rows: list[ProcessedRow]
    position_shares: float
    avg_cost: float
    realized_pnl: float
    total_buy_cost: float        # 누적 매수금액(Σ buy price×shares) — 수익률 분모
    warnings: list[str]


def _parse_ts(date: str, time: str) -> pd.Timestamp:
    """날짜+시각을 Timestamp로. 시각이 비면 00:00."""
    t = (time or "").strip() or "00:00"
    return pd.Timestamp(f"{date} {t}")


def process_transactions(txns: list[Txn], ticker: str = "") -> JournalResult:
    """단일 종목 거래 리스트를 (날짜,시각,입력순) 정렬 후 평균단가 엔진으로 처리.

    BUY  : avg_cost = (avg_cost*pos + price*shares)/(pos+shares); pos += shares
    SELL : sell = min(shares, pos); realized += (price-avg_cost)*sell; pos -= sell
           (평단 관례상 매도는 avg_cost 불변, pos==0이면 리셋)
    """
    indexed = list(enumerate(txns))
    indexed.sort(key=lambda it: (_parse_ts(it[1]["date"], it[1]["time"]), it[0]))

    pos = avg_cost = realized = total_buy_cost = 0.0
    rows: list[ProcessedRow] = []
    warnings: list[str] = []

    for _, t in indexed:
        ts = _parse_ts(t["date"], t["time"])
        side = str(t["side"]).upper()
        price = float(t["price"])
        shares = float(t["shares"])
        note = ""

        if price <= 0 or shares <= 0:
            note = "가격·수량은 0보다 커야 함 — 건너뜀"
            warnings.append(f"{ts:%Y-%m-%d %H:%M} {side}: {note}")
            rows.append(ProcessedRow(ts, side, price, 0.0, pos, avg_cost, realized, note))
            continue

        if side == BUY:
            avg_cost = (avg_cost * pos + price * shares) / (pos + shares)
            pos += shares
            total_buy_cost += price * shares
            filled = shares
        elif side == SELL:
            filled = min(shares, pos)
            if filled < shares:
                note = f"보유 {pos:g}주 초과 매도 {shares - filled:g}주 무시"
                warnings.append(f"{ts:%Y-%m-%d %H:%M} SELL: {note}")
            realized += (price - avg_cost) * filled
            pos -= filled
            if pos <= 1e-9:
                pos = 0.0
                avg_cost = 0.0
        else:
            note = f"알 수 없는 구분 '{t['side']}' — 건너뜀"
            warnings.append(f"{ts:%Y-%m-%d %H:%M}: {note}")
            rows.append(ProcessedRow(ts, side, price, 0.0, pos, avg_cost, realized, note))
            continue

        rows.append(ProcessedRow(ts, side, price, filled, pos, avg_cost, realized, note))

    return JournalResult(
        ticker=ticker, rows=rows, position_shares=pos, avg_cost=avg_cost,
        realized_pnl=realized, total_buy_cost=total_buy_cost, warnings=warnings,
    )


def process_portfolio(txns: list[Txn]) -> dict[str, JournalResult]:
    """여러 종목 거래를 티커별로 그룹핑해 종목마다 process_transactions 실행.

    반환: {ticker: JournalResult} (티커 알파벳 순).
    """
    by_ticker: dict[str, list[Txn]] = {}
    for t in txns:
        tk = str(t.get("ticker", "")).strip().upper()
        if not tk:
            continue
        by_ticker.setdefault(tk, []).append(t)
    return {tk: process_transactions(by_ticker[tk], ticker=tk) for tk in sorted(by_ticker)}


def _pos_avg_before(rows: list[ProcessedRow], ts: pd.Timestamp) -> tuple[float, float, float]:
    """ts 시점(해당 거래 처리 후)까지 반영된 (pos, avg_cost, realized) 스냅샷."""
    pos = avg = realized = 0.0
    for r in rows:
        if r.ts <= ts:
            pos, avg, realized = r.position_shares, r.avg_cost, r.realized_pnl
        else:
            break
    return pos, avg, realized


def build_pnl_curve(
    rows: list[ProcessedRow],
    price_frame: pd.DataFrame | None,
) -> pd.DataFrame:
    """단일 종목 누적손익(금액) 곡선. index=Timestamp, columns=['누적손익'].

    - 거래 이벤트 점: realized + (체결가-avg_cost)*pos.
    - 일별 종가 평가 점: 첫~마지막 거래일 사이 거래일에 보유분 있으면 조정종가로 미실현 평가.
    price_frame이 None이면 거래 이벤트 점만(순수 수동).
    """
    if not rows:
        return pd.DataFrame(columns=["누적손익"])

    points: dict[pd.Timestamp, float] = {}
    for r in rows:
        points[r.ts] = r.realized_pnl + (r.price - r.avg_cost) * r.position_shares

    if price_frame is not None and not price_frame.empty:
        first_day = rows[0].ts.normalize()
        last_day = rows[-1].ts.normalize()
        closes = price_frame.loc[first_day:last_day, "Close"]
        for day, close in closes.items():
            day_close_ts = pd.Timestamp(day).normalize() + pd.Timedelta(hours=23, minutes=59)
            pos, avg, realized = _pos_avg_before(rows, day_close_ts)
            if pos > 0:
                points.setdefault(day_close_ts, realized + (float(close) - avg) * pos)

    s = pd.Series(points).sort_index()
    s.name = "누적손익"
    return s.to_frame()


def portfolio_pnl_curve(
    results: dict[str, JournalResult],
    price_frames: dict[str, pd.DataFrame | None],
) -> pd.DataFrame:
    """종목별 누적손익 곡선을 날짜축 합산 → 포트 전체 누적손익. columns=['누적손익'].

    종목마다 거래 타임스탬프가 달라 outer-join 후 forward-fill(보유 손익은 다음 갱신까지 유지),
    그래도 비어있는(거래 시작 전) 구간은 0으로.
    """
    curves = []
    for tk, res in results.items():
        c = build_pnl_curve(res.rows, price_frames.get(tk))
        if not c.empty:
            curves.append(c["누적손익"].rename(tk))
    if not curves:
        return pd.DataFrame(columns=["누적손익"])
    wide = pd.concat(curves, axis=1).sort_index().ffill().fillna(0.0)
    total = wide.sum(axis=1)
    total.name = "누적손익"
    return total.to_frame()


def unrealized_pnl(position_shares: float, avg_cost: float, last_close: float | None) -> float:
    """보유분 미실현손익 = (최신 종가 - 평단) * 보유수량."""
    if position_shares <= 0 or last_close is None:
        return 0.0
    return (last_close - avg_cost) * position_shares


def portfolio_summary(
    results: dict[str, JournalResult],
    last_closes: dict[str, float | None],
) -> pd.DataFrame:
    """종목별 요약 표. 컬럼: 보유수량/평균단가/실현손익/미실현손익/총손익/매수금액/수익률%."""
    recs = []
    for tk, res in results.items():
        lc = last_closes.get(tk)
        unreal = unrealized_pnl(res.position_shares, res.avg_cost, lc)
        total = res.realized_pnl + unreal
        ret = (total / res.total_buy_cost * 100.0) if res.total_buy_cost > 0 else float("nan")
        recs.append({
            "티커": tk,
            "보유수량": res.position_shares,
            "평균단가": res.avg_cost,
            "실현손익": res.realized_pnl,
            "미실현손익": unreal,
            "총손익": total,
            "매수금액": res.total_buy_cost,
            "수익률%": ret,
        })
    return pd.DataFrame(recs)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 워크 예시: 두 종목, 한 종목은 단타 물타기+분할매도
    sample: list[Txn] = [
        {"date": "2026-06-01", "time": "09:30", "ticker": "AAPL", "side": "BUY",  "price": 150.0, "shares": 100},
        {"date": "2026-06-01", "time": "10:00", "ticker": "AAPL", "side": "BUY",  "price": 140.0, "shares": 50},
        {"date": "2026-06-01", "time": "14:00", "ticker": "AAPL", "side": "SELL", "price": 160.0, "shares": 80},
        {"date": "2026-06-02", "time": "09:31", "ticker": "NVDA", "side": "BUY",  "price": 100.0, "shares": 10},
        {"date": "2026-06-03", "time": "15:00", "ticker": "NVDA", "side": "SELL", "price": 120.0, "shares": 10},
    ]
    results = process_portfolio(sample)
    last_closes = {"AAPL": 165.0, "NVDA": 130.0}

    print("=== 포트 요약 ===")
    print(portfolio_summary(results, last_closes).round(4).to_string(index=False))

    for tk, res in results.items():
        if res.warnings:
            print(f"\n[{tk}] 경고:")
            for w in res.warnings:
                print(f"  - {w}")

    # 검산
    aapl_avg = (150 * 100 + 140 * 50) / 150
    print(f"\n검산 AAPL 평단={aapl_avg:.4f}, 실현={ (160-aapl_avg)*80 :.2f} "
          f"(기대 1066.67)")
    print(f"검산 NVDA 실현={(120-100)*10:.2f} (기대 200.00), 보유 0")

    curve = portfolio_pnl_curve(results, {tk: None for tk in results})
    print("\n[포트 누적손익 — 거래 이벤트 점(가격 None)]")
    print(curve.round(2))
