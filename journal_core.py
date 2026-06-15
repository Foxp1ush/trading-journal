"""매매일지 코어 — 여러 종목 평균단가 엔진 + 하이브리드 누적손익 곡선.

stock-trending-bot의 src/sim/journal.py(단일 종목)를 포팅하고, core 의존을 제거해
가격 프레임을 인자로 주입받도록 했다. 여기에 '여러 종목' 집계(process_portfolio)와
포트 전체 누적손익 곡선(portfolio_pnl_curve)을 추가한다.

원가는 평균단가(평단) 방식 — 국내 증권사 표시와 동일.
순수 로직(Streamlit·네트워크 비의존). 가격은 호출자가 prices.py로 받아 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TypedDict

import pandas as pd

BUY = "BUY"
SELL = "SELL"
DEPOSIT = "DEPOSIT"    # 현금 입금 (ticker 없음, price=금액)
WITHDRAW = "WITHDRAW"  # 현금 출금 (ticker 없음, price=금액)
CASH_SIDES = (DEPOSIT, WITHDRAW)

USD = "USD"
KRW = "KRW"


class Txn(TypedDict):
    """거래 한 건. side는 'BUY'|'SELL'|'DEPOSIT'|'WITHDRAW'.

    매매(BUY/SELL): ticker·price·shares 모두 양수. 통화는 티커로 자동 판정.
    현금(DEPOSIT/WITHDRAW): ticker 빈 문자열, price=금액, shares 무시. 통화는 'currency' 명시.
    """
    date: str    # 'YYYY-MM-DD'
    time: str    # 'HH:MM'
    ticker: str
    side: str
    price: float
    shares: float
    currency: str  # 'USD'|'KRW' — 현금 거래에서 사용(매매는 티커로 자동)


# ---------- 통화 ----------

def currency_of_ticker(ticker: str) -> str:
    """티커 접미사로 통화 판정. .KS(코스피)/.KQ(코스닥) → KRW, 그 외 → USD."""
    t = str(ticker).strip().upper()
    return KRW if (t.endswith(".KS") or t.endswith(".KQ")) else USD


def txn_currency(txn: Txn) -> str:
    """거래의 통화. 현금이면 명시 통화(currency), 매매면 티커로 판정."""
    side = str(txn.get("side", "")).upper()
    if side in CASH_SIDES:
        return str(txn.get("currency") or USD).upper()
    return currency_of_ticker(txn.get("ticker", ""))


def split_by_currency(txns: list[Txn]) -> dict[str, list[Txn]]:
    """거래를 통화별로 분리. 각 묶음은 단일통화라 기존 함수로 그대로 처리 가능."""
    out: dict[str, list[Txn]] = {}
    for t in txns:
        out.setdefault(txn_currency(t), []).append(t)
    return out


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


# 같은 시각(타임스탬프 동률)일 때 처리 순서: 입금 → 매수 → 매도 → 출금.
# 특히 같은 날 BUY/SELL의 시각이 같거나 비어 있으면 '매수 먼저' 처리해야
# 보유수량이 초과매도로 어긋나지 않는다(같은 순간에 사기 전에 팔 수는 없음).
_SIDE_ORDER = {DEPOSIT: 0, BUY: 1, SELL: 2, WITHDRAW: 3}


def _side_priority(side: str) -> int:
    return _SIDE_ORDER.get(str(side).strip().upper(), 1)


def _sort_key(item: tuple[int, Txn]):
    """(원래입력순서, 거래) → 정렬 키 (시각, 같은시각이면 매수먼저, 입력순)."""
    idx, t = item
    return (_parse_ts(t["date"], t["time"]), _side_priority(t["side"]), idx)


def process_transactions(txns: list[Txn], ticker: str = "") -> JournalResult:
    """단일 종목 거래 리스트를 (날짜,시각,입력순) 정렬 후 평균단가 엔진으로 처리.

    BUY  : avg_cost = (avg_cost*pos + price*shares)/(pos+shares); pos += shares
    SELL : sell = min(shares, pos); realized += (price-avg_cost)*sell; pos -= sell
           (평단 관례상 매도는 avg_cost 불변, pos==0이면 리셋)
    """
    indexed = sorted(enumerate(txns), key=_sort_key)

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
    """종목별 요약 표.

    컬럼: 보유수량/평균단가/보유원가/실현손익/미실현손익/총손익/누적매수금액/수익률%.
    - 보유원가 = 현재 보유수량 × 평단 (지금 들고 있는 물량의 원가).
    - 누적매수금액 = Σ(매수 가격×수량) — 이미 팔린 물량 포함. 수익률 분모.
    """
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
            "보유원가": res.position_shares * res.avg_cost,
            "실현손익": res.realized_pnl,
            "미실현손익": unreal,
            "총손익": total,
            "누적매수금액": res.total_buy_cost,
            "수익률%": ret,
        })
    return pd.DataFrame(recs)


# ---------- 현금 / 계좌가치 (입출금 포함) ----------
#
# 현금흐름: BUY -price*체결수량 / SELL +price*체결수량 / DEPOSIT +price / WITHDRAW -price.
# 매도 체결수량은 보유 한도로 캡(초과매도 무시) — 손익 엔진과 동일 규칙이라 현금·수량이 안 어긋남.

def replay(txns: list[Txn]) -> tuple[float, dict[str, float], list[tuple[pd.Timestamp, float, dict[str, float]]]]:
    """전체 거래(매매+현금)를 시간순 처리해 (최종현금, 최종보유, 타임라인) 반환.

    타임라인: 각 거래 처리 직후 (ts, cash, positions 사본) — 계좌가치 곡선의 시점 상태원.
    """
    indexed = sorted(enumerate(txns), key=_sort_key)

    cash = 0.0
    positions: dict[str, float] = {}
    timeline: list[tuple[pd.Timestamp, float, dict[str, float]]] = []

    for _, t in indexed:
        ts = _parse_ts(t["date"], t["time"])
        side = str(t["side"]).upper()
        price = float(t["price"])
        tk = str(t.get("ticker", "")).strip().upper()

        if side == DEPOSIT:
            cash += price
        elif side == WITHDRAW:
            cash -= price
        elif side == BUY:
            shares = float(t.get("shares") or 0)
            if shares > 0 and price > 0 and tk:
                cash -= price * shares
                positions[tk] = positions.get(tk, 0.0) + shares
        elif side == SELL:
            shares = float(t.get("shares") or 0)
            if shares > 0 and price > 0 and tk:
                filled = min(shares, positions.get(tk, 0.0))
                cash += price * filled
                positions[tk] = positions.get(tk, 0.0) - filled
        timeline.append((ts, cash, dict(positions)))

    return cash, positions, timeline


def _price_asof(frame: pd.DataFrame | None, day: pd.Timestamp) -> float | None:
    """day(그날 종가) 기준 조정종가. 그날 없으면 직전, 더 이전도 없으면 최초 종가로 폴백."""
    if frame is None or frame.empty:
        return None
    cutoff = day.normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    sub = frame.loc[:cutoff]
    if not sub.empty:
        return float(sub["Close"].iloc[-1])
    return float(frame["Close"].iloc[0])  # 거래가 데이터 시작보다 앞설 때만


def account_value_curve(
    txns: list[Txn],
    price_frames: dict[str, pd.DataFrame | None],
) -> pd.DataFrame:
    """일별 계좌가치 곡선. index=거래일, column='계좌가치' = 현금 + Σ 보유수량×그날 종가."""
    _, _, timeline = replay(txns)
    if not timeline:
        return pd.DataFrame(columns=["계좌가치"])

    first_day = timeline[0][0].normalize()
    last_day = timeline[-1][0].normalize()
    for f in price_frames.values():
        if f is not None and not f.empty:
            last_day = max(last_day, f.index.max().normalize())

    # 일자 그리드: 구간 내 가격 거래일 ∪ 거래 발생일
    days: set[pd.Timestamp] = {ts.normalize() for ts, _, _ in timeline}
    for f in price_frames.values():
        if f is not None and not f.empty:
            for d in f.loc[first_day:last_day].index:
                days.add(pd.Timestamp(d).normalize())
    grid = sorted(d for d in days if first_day <= d <= last_day)

    def _state_asof(day: pd.Timestamp) -> tuple[float, dict[str, float]]:
        day_end = day.normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
        cash, pos = 0.0, {}
        for ts, c, p in timeline:
            if ts <= day_end:
                cash, pos = c, p
            else:
                break
        return cash, pos

    vals: dict[pd.Timestamp, float] = {}
    for day in grid:
        cash, pos = _state_asof(day)
        mv = 0.0
        for tk, sh in pos.items():
            if sh <= 0:
                continue
            p = _price_asof(price_frames.get(tk), day)
            if p is not None:
                mv += sh * p
        vals[day] = cash + mv

    s = pd.Series(vals).sort_index()
    s.name = "계좌가치"
    return s.to_frame()


def current_account(
    txns: list[Txn],
    last_closes: dict[str, float | None],
) -> dict:
    """현재 스냅샷: 현금잔고·보유시가·계좌가치 + 자산배분(종목 시가 + 현금)."""
    cash, positions, _ = replay(txns)
    holdings_value = 0.0
    allocation: dict[str, float] = {}
    for tk, sh in positions.items():
        if sh <= 0:
            continue
        lc = last_closes.get(tk)
        mv = sh * lc if lc is not None else 0.0
        holdings_value += mv
        if mv > 0:
            allocation[tk] = mv
    if cash > 0:
        allocation["현금"] = cash
    return {
        "cash": cash,
        "holdings_value": holdings_value,
        "account_value": cash + holdings_value,
        "allocation": allocation,
        "positions": positions,
    }


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

    # 현금/계좌가치 워크 예시: 입금 20000 → AAPL 100@150 매수 → 80@160 매도
    cash_sample: list[Txn] = [
        {"date": "2026-06-01", "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 20000, "shares": 0},
        {"date": "2026-06-01", "time": "09:30", "ticker": "AAPL", "side": "BUY",  "price": 150, "shares": 100},
        {"date": "2026-06-01", "time": "14:00", "ticker": "AAPL", "side": "SELL", "price": 160, "shares": 80},
    ]
    acc = current_account(cash_sample, {"AAPL": 165.0})
    print("\n=== 현금/계좌 ===")
    print(f"현금잔고={acc['cash']:.2f} (기대 17800), 보유시가={acc['holdings_value']:.2f} (기대 3300), "
          f"계좌가치={acc['account_value']:.2f} (기대 21100)")
    print(f"자산배분={ {k: round(v,1) for k,v in acc['allocation'].items()} }")
