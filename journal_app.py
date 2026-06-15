"""주식 매매일지 — 여러 종목 평균단가·누적손익 (Streamlit + 각자 본인 구글시트).

배포: Streamlit Community Cloud (공개 URL). 친구는 링크만 클릭.
저장: 각 사용자 본인 구글시트(서비스 계정에 공유). 비공개·영속성은 본인 구글 계정이 책임.

로컬 실행: streamlit run journal_app.py   (.streamlit/secrets.toml 필요)

구조(Phase 1): 메인을 3개 탭으로 분리.
- 📊 대시보드 : 저장된 거래(loaded_df)만 읽어 계좌가치·자산배분·누적손익 렌더.
- ✍️ 매매 기록 : @st.fragment로 격리된 입력 영역(검색·CSV·입력 표). 표는 st.form 안 →
                💾 저장(submit) 때만 시트 저장 + 전체 앱 갱신. 입력 중 다른 탭은 재계산 안 됨.
- 📜 히스토리 : 시트에서 불러온 전체 확정 거래 표.
편집 중 작업본은 draft_df, 저장 확정본은 loaded_df로 분리해 깜빡임(전체 리렌더)을 끊는다.
"""

from __future__ import annotations

from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

import journal_core as jc
import krx
import prices
import sheets

st.set_page_config(page_title="주식 매매일지", page_icon="📒", layout="wide")
st.title("📒 주식 매매일지")
st.caption(
    "실제 체결가를 직접 입력 → 종목별 평균단가·실현손익 + 입출금까지 기록하면 실제 계좌가치·자산배분까지. "
    "보유분은 일별 종가로 평가(하이브리드). 수수료·세금 미반영, 투자 자문 아님."
)


# ---------- 입력 스키마/상수 ----------
# 컬럼 스키마·현금 구분은 단일 출처(sheets.HEADER / journal_core.CASH_SIDES)를 재사용한다.

# 매매 사유 태그(심리 피드백 분류). 빈 값=미분류.
TAGS = ["", "계획", "추격", "충동", "패닉", "분할", "리밸런싱", "기타"]

TEMPLATE_CSV = (
    "date,time,ticker,side,price,shares,currency,tag,note\n"
    "2026-06-01,09:00,,DEPOSIT,1000,,USD,,월급 입금\n"
    "2026-06-01,09:30,AAPL,BUY,150,10,USD,계획,실적 기대 분할매수1\n"
    "2026-06-02,14:00,AAPL,SELL,160,4,USD,계획,목표가 도달 일부 익절\n"
    "2026-06-03,09:05,,DEPOSIT,1000000,,KRW,,\n"
    "2026-06-03,09:30,005930.KS,BUY,70000,10,KRW,추격,급등 따라 매수\n"
)


# ---------- 차트 ----------

_PNL_AUTOSIZE = {"type": "fit-y", "contains": "padding"}


def _pnl_layer_list(df: pd.DataFrame, y_title: str) -> list[alt.Chart]:
    """누적손익 라인 + Y=0 기준선 + 호버 안내선의 개별 레이어들(평면 layer용)."""
    long = (
        df.rename_axis("날짜").reset_index()
        .melt(id_vars="날짜", var_name="계열", value_name="값")
    )
    zoom = alt.selection_interval(bind="scales", encodings=["x"])
    hover = alt.selection_point(nearest=True, on="pointerover", fields=["날짜"], empty=False)
    base = alt.Chart(long).encode(
        x=alt.X("날짜:T", title=None, axis=alt.Axis(labelAngle=-45, labelLimit=80)),
        y=alt.Y("값:Q", title=y_title),
        color=alt.Color("계열:N", title=None),
    )
    line = base.mark_line().add_params(zoom)
    points = base.mark_point(size=60, filled=True).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    guide = (
        alt.Chart(long).transform_pivot("계열", value="값", groupby=["날짜"])
        .mark_rule(color="gray")
        .encode(
            x="날짜:T",
            opacity=alt.condition(hover, alt.value(0.3), alt.value(0)),
            tooltip=[alt.Tooltip("날짜:T", title="날짜")]
            + [alt.Tooltip(c, type="quantitative", title=c, format="+.2f")
               for c in (str(x) for x in df.columns)],
        )
        .add_params(hover)
    )
    zero = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(
        color="#333", strokeWidth=2.5).encode(y="y:Q")
    return [line, zero, points, guide]


def _line_with_zero(df: pd.DataFrame, y_title: str) -> alt.LayerChart:
    """누적손익 라인 + Y=0 기준선 + 호버 안내선 + 휠 줌."""
    return alt.layer(*_pnl_layer_list(df, y_title)).properties(
        height=360, autosize=_PNL_AUTOSIZE
    )


def _trade_markers(rows) -> pd.DataFrame:
    """누적손익 곡선에 얹을 매수/매도 마커 데이터. 실제 체결된 매매행만(현금·미체결 제외).

    y값은 build_pnl_curve의 거래 이벤트 점과 동일 공식 → 라인 위 정확한 위치에 안착.
    """
    recs = []
    for r in rows:
        s = str(r.side).upper()
        if s not in ("BUY", "SELL") or r.shares <= 0:
            continue
        y = r.realized_pnl + (r.price - r.avg_cost) * r.position_shares
        recs.append({"날짜": r.ts, "값": y, "거래": "매수" if s == "BUY" else "매도",
                     "가격": r.price, "수량": r.shares})
    return pd.DataFrame(recs)


def _pnl_chart_with_trades(detail_df: pd.DataFrame, rows, y_title: str) -> alt.LayerChart:
    """종목별 누적손익 라인 + 매수(초록 ▲)·매도(빨강 ▼) 시점 마커."""
    layers = _pnl_layer_list(detail_df, y_title)
    mk = _trade_markers(rows)
    if mk.empty:
        return alt.layer(*layers).properties(height=360, autosize=_PNL_AUTOSIZE)
    layers.append(
        alt.Chart(mk)
        .mark_point(size=140, filled=True, opacity=0.95, stroke="white", strokeWidth=1)
        .encode(
            x="날짜:T", y="값:Q",
            color=alt.Color("거래:N", title="거래",
                            scale=alt.Scale(domain=["매수", "매도"], range=["#2ca02c", "#d62728"])),
            shape=alt.Shape("거래:N", title="거래",
                            scale=alt.Scale(domain=["매수", "매도"], range=["triangle-up", "triangle-down"])),
            tooltip=[alt.Tooltip("날짜:T", title="시점"), alt.Tooltip("거래:N", title="구분"),
                     alt.Tooltip("가격:Q", title="체결가", format=",.2f"),
                     alt.Tooltip("수량:Q", title="수량", format="g"),
                     alt.Tooltip("값:Q", title="누적손익", format="+.2f")],
        )
    )
    # 라인은 '계열'(종목명), 마커는 '거래'(매수/매도)로 색을 쓰므로 색 스케일을 레이어별 독립으로.
    # (공유하면 마커 색 도메인에 종목명이 없어 라인이 안 보이게 됨)
    return (
        alt.layer(*layers)
        .resolve_scale(color="independent", shape="independent")
        .properties(height=360, autosize=_PNL_AUTOSIZE)
    )


def _line_chart(df: pd.DataFrame, y_title: str) -> alt.Chart:
    """0선 없는 단순 라인(계좌가치처럼 0 기준이 무의미한 값용). 휠 줌 + 호버 툴팁."""
    col = str(df.columns[0])
    data = df.rename_axis("날짜").reset_index()
    zoom = alt.selection_interval(bind="scales", encodings=["x"])
    return (
        alt.Chart(data)
        .mark_line(point=True)
        .encode(
            x=alt.X("날짜:T", title=None, axis=alt.Axis(labelAngle=-45, labelLimit=80)),
            y=alt.Y(f"{col}:Q", title=y_title, scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("날짜:T", title="날짜"),
                     alt.Tooltip(f"{col}:Q", title=y_title, format=",.0f")],
        )
        .add_params(zoom)
        .properties(height=320, autosize=_PNL_AUTOSIZE)
    )


def _pie_chart(allocation: dict[str, float]) -> alt.Chart:
    """자산배분 도넛 — 종목 시가 + 현금 비중."""
    data = pd.DataFrame({"항목": list(allocation), "금액": list(allocation.values())})
    total = data["금액"].sum()
    data["비중"] = data["금액"] / total * 100.0 if total > 0 else 0.0
    return (
        alt.Chart(data)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta("금액:Q", stack=True),
            color=alt.Color("항목:N", title=None),
            tooltip=["항목:N", alt.Tooltip("금액:Q", format=",.0f"),
                     alt.Tooltip("비중:Q", format=".1f")],
        )
        .properties(height=300)
    )


CCY_LABEL = {"USD": "💵 달러 계좌 (USD)", "KRW": "🇰🇷 원화 계좌 (KRW)"}


def fmt_money(x: float, ccy: str) -> str:
    """통화 기호·자릿수에 맞춘 금액 표기. USD 2자리, KRW 정수."""
    if ccy == "KRW":
        return f"₩{x:,.0f}"
    if ccy == "USD":
        return f"${x:,.2f}"
    return f"{x:,.2f} {ccy}"


# ---------- 데이터 변환 헬퍼 ----------

def df_to_txns(df: pd.DataFrame | None) -> list[dict]:
    """거래 표(DataFrame) → Txn 리스트. 현금(DEPOSIT/WITHDRAW)/매매 분기, 빈값 스킵."""
    txns: list[dict] = []
    if df is None or len(df) == 0:
        return txns
    for _, row in df.iterrows():
        d = row.get("date")
        price = row.get("price")
        if pd.isna(d) or pd.isna(price):
            continue
        side = str(row.get("side") or "BUY").upper()
        tk = str(row.get("ticker") or "").strip().upper()
        ccy = str(row.get("currency") or "USD").strip().upper() or "USD"
        base = {
            "date": pd.Timestamp(d).strftime("%Y-%m-%d"),
            "time": str(row.get("time") or "09:30"),
            "tag": str(row.get("tag") or ""),
        }
        if side in jc.CASH_SIDES:
            txns.append({**base, "ticker": "", "side": side,
                         "price": float(price), "shares": 0.0, "currency": ccy})
        else:  # BUY / SELL — 티커·수량 필요 (통화는 티커로 자동 판정)
            if not tk or pd.isna(row.get("shares")):
                continue
            txns.append({**base, "ticker": tk, "side": side,
                         "price": float(price), "shares": float(row["shares"]), "currency": ccy})
    return txns


def build_manual_row(d, time: str, ticker: str, side: str,
                     price, shares, ccy: str, tag: str = "", note: str = "") -> tuple[dict | None, str]:
    """빠른 입력 폼 한 건을 검증해 시트 1행(sheets.HEADER 모양) 또는 (None, 에러문구) 반환.

    가격>0 필수. 매매(BUY/SELL)는 티커·수량(>0) 필수. 현금(DEPOSIT/WITHDRAW)은 티커/수량 무시.
    tag/note는 메타데이터(검증 없음).
    """
    side = str(side or "").upper()
    if price is None or float(price) <= 0:
        return None, "가격/금액을 입력하세요(0보다 커야 함)."
    cash = side in jc.CASH_SIDES
    tk = "" if cash else str(ticker or "").strip().upper()
    if not cash:
        if not tk:
            return None, "매매(BUY/SELL)는 티커가 필요합니다."
        if shares is None or float(shares) <= 0:
            return None, "매매(BUY/SELL)는 수량이 필요합니다."
    row = {
        "date": pd.Timestamp(d).strftime("%Y-%m-%d"),
        "time": (str(time).strip() or "09:30") if time else "09:30",
        "ticker": tk,
        "side": side,
        "price": float(price),
        "shares": (None if cash else float(shares)),
        "currency": str(ccy or "USD").upper(),
        "tag": str(tag or ""),
        "note": str(note or ""),
    }
    return row, ""


def prepare_edit_df(loaded: pd.DataFrame | None) -> pd.DataFrame:
    """시트·CSV·draft의 문자열을 data_editor 컬럼 타입에 맞게 정리(+빈 표는 예시 행 시드)."""
    if loaded is None or len(loaded) == 0:
        today = date.today()
        loaded = pd.DataFrame([
            {"date": str(today), "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 1000.0, "shares": None, "currency": "USD", "tag": "", "note": ""},
            {"date": str(today), "time": "09:30", "ticker": "AAPL", "side": "BUY", "price": 150.0, "shares": 10.0, "currency": "USD", "tag": "계획", "note": ""},
        ])
    edit_df = loaded.copy()
    for c in ("currency", "tag", "note"):
        if c not in edit_df.columns:
            edit_df[c] = "USD" if c == "currency" else ""
    for c in ("ticker", "side", "time", "currency", "tag", "note"):
        if c in edit_df.columns:
            edit_df[c] = edit_df[c].astype(str).replace({"nan": "", "None": ""})
    edit_df["currency"] = edit_df["currency"].replace({"": "USD"}).str.upper()
    edit_df["date"] = pd.to_datetime(edit_df["date"], errors="coerce")
    edit_df["price"] = pd.to_numeric(edit_df["price"], errors="coerce")
    edit_df["shares"] = pd.to_numeric(edit_df["shares"], errors="coerce")
    return edit_df


def _persist_trades(df: pd.DataFrame) -> int:
    """거래 DataFrame을 시트에 저장하고 세션 상태(loaded_df/draft_df) 동기화. 저장 건수 반환.

    빠른 입력·전체 표 저장이 공통으로 사용한다.
    """
    save_df = df.copy()
    save_df["date"] = pd.to_datetime(save_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    ws = sheets.connect(st.session_state["sheet_url"])
    n = sheets.save_trades(ws, save_df)
    st.session_state["loaded_df"] = sheets.load_trades(ws)
    st.session_state["draft_df"] = st.session_state["loaded_df"].copy()
    st.session_state["editor_ver"] = st.session_state.get("editor_ver", 0) + 1
    return n


def _account_metrics(ccy: str, account: dict) -> None:
    """계좌가치·현금잔고·보유시가 3-metric."""
    a = st.columns(3)
    a[0].metric("계좌가치", fmt_money(account["account_value"], ccy), help="현금 + 보유 시가평가")
    a[1].metric("현금잔고", fmt_money(account["cash"], ccy), help="입금·매도 − 출금·매수")
    a[2].metric("보유 시가", fmt_money(account["holdings_value"], ccy))


def _allocation_and_value(ccy: str, ccy_txns: list[dict], account: dict,
                          price_frames: dict) -> None:
    """자산배분 도넛 + 계좌가치 추이 곡선(기간 필터)."""
    col_pie, col_curve = st.columns([1, 2])
    with col_pie:
        st.caption("자산배분")
        if account["allocation"]:
            st.altair_chart(_pie_chart(account["allocation"]), width="stretch")
        else:
            st.info("보유 종목·현금이 없습니다.")

    acct_curve = jc.account_value_curve(ccy_txns, price_frames)
    with col_curve:
        st.caption("계좌가치 추이")
        if acct_curve.empty:
            st.info("표시할 계좌가치 데이터가 없습니다.")
        else:
            dmin, dmax = acct_curve.index.min().date(), acct_curve.index.max().date()
            c1, c2 = st.columns(2)
            start = c1.date_input("시작", value=dmin, min_value=dmin, max_value=dmax, key=f"avs_{ccy}")
            end = c2.date_input("끝", value=dmax, min_value=dmin, max_value=dmax, key=f"ave_{ccy}")
            win = acct_curve.loc[str(start):str(end)]
            if win.empty:
                win = acct_curve
            st.altair_chart(_line_chart(win, "계좌가치"), width="stretch")
            change = float(win["계좌가치"].iloc[-1] - win["계좌가치"].iloc[0])
            st.metric("선택 기간 계좌가치 변화", fmt_money(change, ccy))


def _pnl_table(ccy: str, results: dict, last_closes: dict) -> None:
    """종목별 손익 요약 metric + 표(KRW는 한글 종목명 병기)."""
    st.subheader("매매 손익 — 종목별")
    summary = jc.portfolio_summary(results, last_closes)
    if ccy == "KRW" and "티커" in summary.columns:  # 한글 종목명 병기(표시용)
        summary.insert(1, "종목명", summary["티커"].map(lambda t: krx.resolve_name(t) or ""))
    m = st.columns(4)
    m[0].metric("실현손익 합계", fmt_money(summary["실현손익"].sum(), ccy))
    m[1].metric("미실현손익 합계", fmt_money(summary["미실현손익"].sum(), ccy))
    m[2].metric("총손익 합계", fmt_money(summary["총손익"].sum(), ccy))
    tot_cost = summary["누적매수금액"].sum()
    tot_ret = (summary["총손익"].sum() / tot_cost * 100.0) if tot_cost > 0 else 0.0
    m[3].metric("수익률", f"{tot_ret:+.2f}%", help="총손익 합계 / 누적 매수금액(매도분 포함)")

    money = "{:,.0f}" if ccy == "KRW" else "{:,.2f}"
    signed = "{:+,.0f}" if ccy == "KRW" else "{:+,.2f}"
    st.caption(
        "**보유원가** = 현재 보유수량 × 평단(지금 들고 있는 물량 원가). "
        "**누적매수금액** = 지금까지 매수에 쓴 총액(이미 판 물량 포함, 수익률 분모)."
    )
    st.dataframe(
        summary.style.format({
            "보유수량": "{:g}", "평균단가": money, "보유원가": money, "실현손익": signed,
            "미실현손익": signed, "총손익": signed, "누적매수금액": money, "수익률%": "{:+.2f}",
        }),
        width="stretch",
    )


def _pnl_curves(ccy: str, results: dict, price_frames: dict) -> None:
    """통화 전체 누적손익 곡선 + 선택 종목 상세(매수/매도 마커) + 거래 처리 내역."""
    curve = jc.portfolio_pnl_curve(results, price_frames)
    if not curve.empty:
        st.altair_chart(_line_with_zero(curve, f"누적손익 ({ccy})"), width="stretch")

    pick = st.selectbox("종목 선택", options=list(results), key=f"pick_{ccy}")
    res = results[pick]
    detail = jc.build_pnl_curve(res.rows, price_frames.get(pick))
    if not detail.empty:
        st.altair_chart(
            _pnl_chart_with_trades(detail.rename(columns={"누적손익": pick}), res.rows, f"누적손익 ({ccy})"),
            width="stretch",
        )
    for w in res.warnings:
        st.warning(w)
    with st.expander("거래 처리 내역 (평단·실현손익 추적)"):
        audit = pd.DataFrame([
            {
                "시각": r.ts, "구분": r.side, "가격": r.price, "체결수량": r.shares,
                "보유수량": r.position_shares, "평균단가": r.avg_cost,
                "누적실현손익": r.realized_pnl, "비고": r.note,
            }
            for r in res.rows
        ])
        st.dataframe(audit, width="stretch")


def _tag_bar(perf: pd.DataFrame, ccy: str) -> alt.Chart:
    """태그별 실현손익 막대(양수 초록/음수 빨강)."""
    return (
        alt.Chart(perf)
        .mark_bar()
        .encode(
            x=alt.X("실현손익:Q", title=f"실현손익 ({ccy})"),
            y=alt.Y("태그:N", sort="-x", title=None),
            color=alt.condition(alt.datum["실현손익"] >= 0, alt.value("#2ca02c"), alt.value("#d62728")),
            tooltip=["태그:N", "매매횟수:Q", "매도횟수:Q",
                     alt.Tooltip("실현손익:Q", format="+,.2f"),
                     alt.Tooltip("평균실현:Q", format="+,.2f"),
                     alt.Tooltip("승률%:Q", format=".0f")],
        )
        .properties(height=max(120, 34 * len(perf)))
    )


def _tag_feedback(ccy: str, results: dict) -> None:
    """🧠 태그별 매매 피드백 — 사유 태그별 빈도·실현손익·승률(심리 패턴 직면)."""
    perf = jc.tag_performance(results)
    if perf.empty:
        return
    st.subheader("🧠 태그별 매매 피드백")
    st.caption(
        "매매 사유 태그별 통계입니다. **실현손익은 매도 시점 태그 기준**(평단 방식상 매수 태그엔 실현 귀속 불가), "
        "**매매횟수**는 매수·매도 모두. 충동·패닉 등 어떤 마음가짐이 돈을 버는지/잃는지 직면하세요. "
        "(메모 원문은 📜 히스토리 탭)"
    )
    st.altair_chart(_tag_bar(perf, ccy), width="stretch")

    signed = "{:+,.0f}" if ccy == "KRW" else "{:+,.2f}"
    st.dataframe(
        perf.style.format({"실현손익": signed, "평균실현": signed, "승률%": "{:.0f}"}, na_rep="—"),
        width="stretch",
    )


def render_currency_section(ccy: str, ccy_txns: list[dict]) -> None:
    """한 통화(USD 또는 KRW)의 계좌·손익 섹션 전체를 렌더(블록 헬퍼 조립)."""
    st.header(CCY_LABEL.get(ccy, f"{ccy} 계좌"))

    results = jc.process_portfolio(ccy_txns)
    price_frames = {tk: prices.get_prices(tk) for tk in results}
    last_closes = {tk: prices.latest_close(price_frames[tk]) for tk in results}
    account = jc.current_account(ccy_txns, last_closes)

    missing = [tk for tk in results if last_closes[tk] is None]
    if missing:
        st.warning(f"시세를 못 받은 종목(평가 0 처리): {', '.join(missing)}")

    _account_metrics(ccy, account)
    _allocation_and_value(ccy, ccy_txns, account, price_frames)

    if not results:
        st.info("이 통화의 매매 기록이 없습니다(현금 거래만).")
        return

    _pnl_table(ccy, results, last_closes)
    _pnl_curves(ccy, results, price_frames)
    _tag_feedback(ccy, results)


# ---------- 매매 기록 입력 (Tab 2 — fragment로 격리) ----------

def _quick_add_form() -> None:
    """맨 위 빠른 입력 — 한 거래씩 입력→즉시 시트 저장(스크롤 불필요)."""
    # 위젯 키는 인스턴스화 후 수정 불가 → 프리필·초기화는 위젯 생성 전에 처리한다.
    # 검색에서 고른 종목 프리필
    if "qa_prefill_ticker" in st.session_state:
        st.session_state["qa_ticker"] = st.session_state.pop("qa_prefill_ticker")
        st.session_state["qa_currency"] = st.session_state.pop("qa_prefill_ccy", "USD")
    # 직전 추가 성공 → 티커·가격·수량·태그·메모만 비움(날짜·구분·통화는 연속 입력 위해 유지)
    if st.session_state.pop("qa_clear", False):
        st.session_state["qa_ticker"] = ""
        st.session_state["qa_price"] = None
        st.session_state["qa_shares"] = None
        st.session_state["qa_tag"] = ""
        st.session_state["qa_note"] = ""

    st.subheader("➕ 새 거래 추가")
    st.caption(
        "한 건씩 입력하고 **추가**를 누르면 바로 저장돼 대시보드에 반영됩니다. "
        "매매는 티커·가격·수량을, 입금·출금은 구분만 바꾸고 '가격/금액'에 금액만(티커·수량 비움)."
    )
    with st.form("quick_add"):
        r1 = st.columns([1.2, 1, 1, 1])
        d = r1[0].date_input("날짜", value=date.today(), format="YYYY-MM-DD", key="qa_date")
        t = r1[1].text_input("시각", value="09:30", key="qa_time")
        side = r1[2].selectbox("구분", ["BUY", "SELL", "DEPOSIT", "WITHDRAW"], key="qa_side")
        ccy = r1[3].selectbox("통화", ["USD", "KRW"], key="qa_currency")
        r2 = st.columns([1.6, 1, 1])
        ticker = r2[0].text_input("티커", key="qa_ticker",
                                  placeholder="예: AAPL, 005930.KS (입금·출금은 비움)")
        price = r2[1].number_input("가격/금액", min_value=0.0, value=None, key="qa_price")
        shares = r2[2].number_input("수량(주)", min_value=0.0, value=None, key="qa_shares")
        r3 = st.columns([1, 2.6])
        tag = r3[0].selectbox("태그(사유)", TAGS, key="qa_tag",
                              format_func=lambda x: x or "(미분류)")
        note = r3[1].text_input("메모", key="qa_note", placeholder="왜 샀/팔았는지 — 근거·심리 메모(선택)")
        added = st.form_submit_button("➕ 추가 (즉시 저장)", type="primary")

    if added:
        row, err = build_manual_row(d, t, ticker, side, price, shares, ccy, tag, note)
        if err:
            st.error(err)
            return
        base = st.session_state.get("loaded_df")
        new_df = (pd.DataFrame([row]) if base is None or len(base) == 0
                  else pd.concat([base, pd.DataFrame([row])], ignore_index=True))
        try:
            _persist_trades(new_df)
            st.session_state["qa_clear"] = True  # 다음 런에서 티커·가격·수량 비움
            label = row["ticker"] or row["side"]
            st.toast(f"➕ 추가 완료 — {label}. 대시보드·히스토리에 반영했습니다.", icon="✅")
            st.rerun()  # 전체 갱신
        except Exception as exc:  # noqa: BLE001
            st.error(f"저장 실패: {exc}")


def _search_helper() -> None:
    """종목 검색 → 빠른 입력 폼의 티커·통화 프리필."""
    st.caption("종류를 고르고 검색해 결과를 선택하면 위 '새 거래 추가'의 티커·통화가 채워집니다.")
    market = st.radio("종류", ["한국", "미국"], horizontal=True, key="krx_market")
    ph = "예: 삼성전자, 005930, KODEX 200" if market == "한국" else "예: apple, AAPL, SPY"
    q = st.text_input("종목명 / 코드 / 심볼", key="krx_q", placeholder=ph)
    if q.strip():
        hits = krx.search(q, market={"한국": "KR", "미국": "US"}[market])
        if hits:
            labels = [f"{h['name']} ({h['market']}, {h['currency']}) → {h['ticker']}" for h in hits]
            idx = st.selectbox("후보 선택", range(len(hits)), format_func=lambda i: labels[i], key="krx_pick")
            if st.button("⬆️ 이 종목 선택 (위 폼에 채우기)", key="krx_pick_btn"):
                # 위젯 키가 아닌 prefill 키에 담아둔다(폼은 위에서 이미 인스턴스화됨).
                st.session_state["qa_prefill_ticker"] = hits[idx]["ticker"]
                st.session_state["qa_prefill_ccy"] = hits[idx]["currency"]
                st.rerun(scope="fragment")
        else:
            st.caption("검색 결과가 없습니다. 위 폼에 티커를 직접 입력해도 됩니다(국내 005930.KS / 미국 AAPL).")


def _csv_import() -> None:
    """CSV 업로드 → 전체 표(draft)에 채움(확정은 전체 표에서 저장)."""
    st.caption(
        "양식 CSV를 받아 채운 뒤 업로드하면 아래 '전체 거래' 표에 채워집니다(여러 건 한 번에). "
        "컬럼: date,time,ticker,side,price,shares,currency,tag,note. 입금·출금은 ticker 비우고 "
        "side=DEPOSIT/WITHDRAW, price=금액. tag/note는 비워도 됩니다. 국내 주식은 005930.KS/.KQ 코드."
    )
    st.download_button("양식 CSV 받기", TEMPLATE_CSV, file_name="trades_template.csv", mime="text/csv")
    up = st.file_uploader("CSV 업로드", type=["csv"], key="csv_up")
    if up is None:
        return
    try:
        imported = pd.read_csv(up, dtype=str)
        missing = [c for c in sheets.HEADER if c not in imported.columns]
        if missing:
            st.error(f"필수 컬럼 누락: {missing} — 양식 CSV를 사용하세요.")
            return
        imported = imported[sheets.HEADER]
        st.dataframe(imported, width="stretch")
        if st.button("이 데이터로 표 채우기", key="csv_apply"):
            st.session_state["draft_df"] = imported
            st.session_state["editor_ver"] = st.session_state.get("editor_ver", 0) + 1
            st.success(f"{len(imported)}행을 아래 표에 채웠습니다. 펼쳐 확인 후 💾 저장하세요.")
            st.rerun(scope="fragment")
    except Exception as exc:  # noqa: BLE001
        st.error(f"CSV 읽기 실패: {exc}")


def _balance_reconcile() -> None:
    """예수금 보정 — 입출금 기록이 없을 때 현재 실제 예수금에 맞춰 보정 입출금 1건 추가."""
    st.caption(
        "입출금을 안 적으면 현금이 음수(순매수금액)로 잡힙니다. **현재 실제 예수금**을 넣으면 차액만큼 "
        "**보정 입출금 1건**을 추가해 맞춥니다. 보유시가는 매매 기록에서 이미 반영돼 있어요."
    )
    txns = df_to_txns(st.session_state.get("loaded_df"))
    by_ccy = jc.split_by_currency(txns)
    ccy_options = [c for c in ("USD", "KRW") if c in by_ccy] or ["USD", "KRW"]
    ccy = st.selectbox("통화", ccy_options, key="rec_ccy")

    ccy_txns = by_ccy.get(ccy, [])
    computed = jc.replay(ccy_txns)[0]
    dates = [pd.Timestamp(t["date"]) for t in ccy_txns if t.get("date")]
    default_date = min(dates).date() if dates else date.today()

    c1, c2 = st.columns(2)
    c1.metric("계산상 예수금(현재)", fmt_money(computed, ccy), help="입금·매도 − 출금·매수로 계산된 현금")
    real = c2.number_input("실제 예수금(증권사 화면)", min_value=0.0, value=None, key="rec_real")
    rec_date = st.date_input("보정 거래 날짜", value=default_date, format="YYYY-MM-DD", key="rec_date")

    if real is None:
        st.info("실제 예수금을 입력하면 보정 금액을 계산합니다.")
        return
    adj = float(real) - computed
    if abs(adj) < 1e-9:
        st.success("이미 실제 예수금과 일치합니다 — 보정 불필요.")
        return
    side = jc.DEPOSIT if adj > 0 else jc.WITHDRAW
    st.write(
        f"보정 거래: **{side} {fmt_money(abs(adj), ccy)}** ({rec_date}) "
        f"→ 이후 현금이 {fmt_money(float(real), ccy)}가 됩니다."
    )
    if st.button("⚖️ 보정 거래 추가·저장", key="rec_add", type="primary"):
        row, err = build_manual_row(rec_date, "00:00", "", side, abs(adj), None, ccy,
                                    "", "예수금 보정(입출금 기록 없음)")
        if err:
            st.error(err)
            return
        base = st.session_state.get("loaded_df")
        new_df = (pd.DataFrame([row]) if base is None or len(base) == 0
                  else pd.concat([base, pd.DataFrame([row])], ignore_index=True))
        try:
            _persist_trades(new_df)
            st.toast(f"⚖️ 보정 완료 — {side} {fmt_money(abs(adj), ccy)}", icon="✅")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"저장 실패: {exc}")


def _full_table_editor() -> None:
    """전체 거래 수정·삭제(벌크) — 표 편집 후 💾 저장. 평소엔 접혀 있어 스크롤 부담 없음."""
    st.caption(
        "기존 거래 **수정·삭제**나 CSV로 채운 여러 건 확정용입니다. 고친 뒤 **💾 저장**을 눌러야 반영됩니다. "
        "※ 표를 고치던 중 위 '새 거래 추가'를 저장하면 표의 미저장 편집은 사라질 수 있어요."
    )
    edit_df = prepare_edit_df(st.session_state.get("draft_df"))
    editor_key = f"editor_{st.session_state.get('editor_ver', 0)}"
    with st.form("transaction_form"):
        edited = st.data_editor(
            edit_df, num_rows="dynamic", width="stretch", key=editor_key,
            column_config={
                "date": st.column_config.DateColumn("날짜", format="YYYY-MM-DD", required=True),
                "time": st.column_config.TextColumn("시각", help="HH:MM (24시간)", default="09:30"),
                "ticker": st.column_config.TextColumn("티커", help="입금·출금은 비워두세요. 국내는 005930.KS"),
                "side": st.column_config.SelectboxColumn(
                    "구분", options=["BUY", "SELL", "DEPOSIT", "WITHDRAW"], required=True, default="BUY"),
                "price": st.column_config.NumberColumn(
                    "가격/금액", help="매매는 체결가, 입금·출금은 금액", min_value=0.0, required=True),
                "shares": st.column_config.NumberColumn("수량(주)", help="입금·출금은 비워두세요", min_value=0.0, format="%g"),
                "currency": st.column_config.SelectboxColumn(
                    "통화", options=["USD", "KRW"], help="입금·출금 통화(매매는 티커로 자동)", default="USD"),
                "tag": st.column_config.SelectboxColumn(
                    "태그", options=TAGS, help="매매 사유(계획/충동/패닉 등) — 태그별 통계용", default=""),
                "note": st.column_config.TextColumn("메모", help="근거·심리 메모(선택)"),
            },
        )
        submitted = st.form_submit_button("💾 저장", type="primary")

    if submitted:
        try:
            n = _persist_trades(edited)
            st.toast(f"💾 저장 완료 — {n}건 기록. 대시보드·히스토리에 반영했습니다.", icon="✅")
            st.rerun()  # 전체 갱신
        except Exception as exc:  # noqa: BLE001
            st.error(f"저장 실패: {exc}")


@st.fragment
def record_fragment() -> None:
    """매매 기록 탭 — 빠른 입력(즉시 저장) 위주, 전체 표는 접이식 수정용. fragment로 격리."""
    _quick_add_form()

    with st.expander("🔎 종목 검색 (티커 채우기)", expanded=False):
        _search_helper()

    with st.expander("📥 CSV로 여러 건 가져오기", expanded=False):
        _csv_import()

    with st.expander("⚖️ 예수금 보정 (입출금 기록이 없을 때)", expanded=False):
        _balance_reconcile()

    draft = st.session_state.get("draft_df")
    n_rows = 0 if draft is None else len(draft)
    with st.expander(f"📋 전체 거래 수정·삭제 ({n_rows}건)", expanded=False):
        _full_table_editor()


# ---------- 시트 연결 (탭 밖, 공통 게이트) ----------

with st.expander("① 처음이라면 — 구글시트 연결 방법", expanded=False):
    st.markdown(
        f"""
1. 본인 구글 드라이브에서 **빈 구글시트**를 하나 만듭니다.
2. 그 시트를 아래 **서비스 계정 이메일**에 **편집** 권한으로 공유합니다:

   `{sheets.service_account_email()}`
3. 시트 주소창의 **URL을 복사**해 아래에 붙여넣고 **연결**을 누릅니다.

> 비공개·저장은 **본인 구글시트**가 책임집니다. 앱은 공유된 시트에만 접근합니다.
> (서비스 계정이 공유 시트를 읽을 수 있으니, 운영자로부터의 완전 격리는 아닙니다.)
"""
    )

col_url, col_btn = st.columns([4, 1])
sheet_url = col_url.text_input(
    "내 구글시트 URL", value=st.session_state.get("sheet_url", ""),
    placeholder="https://docs.google.com/spreadsheets/d/.../edit",
)
if col_btn.button("연결", type="primary", use_container_width=True):
    try:
        ws = sheets.connect(sheet_url)
        st.session_state["sheet_url"] = sheet_url
        st.session_state["ws_ok"] = True
        st.session_state["loaded_df"] = sheets.load_trades(ws)
        st.session_state["draft_df"] = st.session_state["loaded_df"].copy()
        st.session_state["editor_ver"] = st.session_state.get("editor_ver", 0) + 1
        st.success("연결됨 — 시트의 거래를 불러왔습니다.")
    except Exception as exc:  # noqa: BLE001
        st.session_state["ws_ok"] = False
        st.error(f"연결 실패: {exc}")

if not st.session_state.get("ws_ok"):
    st.info("구글시트를 연결하면 매매 입력·저장이 활성화됩니다.")
    st.stop()

# 작업본(draft_df) 보정 — 연결됐는데 누락된 경우(세션 재개 등)
if "draft_df" not in st.session_state:
    base = st.session_state.get("loaded_df")
    st.session_state["draft_df"] = base.copy() if base is not None else pd.DataFrame(columns=sheets.HEADER)


# ---------- 메인: 3개 탭 ----------

tab_dash, tab_record, tab_hist = st.tabs(["📊 대시보드", "✍️ 매매 기록", "📜 히스토리"])

with tab_dash:
    txns = df_to_txns(st.session_state.get("loaded_df"))
    if not txns:
        st.info("아직 저장된 거래가 없습니다. **✍️ 매매 기록** 탭에서 입력하고 💾 저장하세요.")
    else:
        by_ccy = jc.split_by_currency(txns)
        # USD 먼저, 그다음 KRW, 나머지는 알파벳 순
        order = ([c for c in ("USD", "KRW") if c in by_ccy]
                 + sorted(c for c in by_ccy if c not in ("USD", "KRW")))
        for i, ccy in enumerate(order):
            if i > 0:
                st.divider()
            render_currency_section(ccy, by_ccy[ccy])

with tab_record:
    record_fragment()

with tab_hist:
    hist = st.session_state.get("loaded_df")
    if hist is None or len(hist) == 0:
        st.info("저장된 거래가 없습니다. **✍️ 매매 기록** 탭에서 입력하고 💾 저장하세요.")
    else:
        st.caption(f"시트에 저장된 전체 확정 거래 {len(hist)}건.")
        st.dataframe(hist, width="stretch", hide_index=True)
        st.download_button(
            "CSV로 내보내기", hist.to_csv(index=False).encode("utf-8-sig"),
            file_name="trades_export.csv", mime="text/csv",
        )
