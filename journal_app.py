"""주식 매매일지 — 여러 종목 평균단가·누적손익 (Streamlit + 각자 본인 구글시트).

배포: Streamlit Community Cloud (공개 URL). 친구는 링크만 클릭.
저장: 각 사용자 본인 구글시트(서비스 계정에 공유). 비공개·영속성은 본인 구글 계정이 책임.

로컬 실행: streamlit run journal_app.py   (.streamlit/secrets.toml 필요)
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


# ---------- 차트 ----------

def _line_with_zero(df: pd.DataFrame, y_title: str) -> alt.LayerChart:
    """누적손익 라인 + Y=0 기준선 + 호버 안내선 + 휠 줌."""
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
    return alt.layer(line, zero, points, guide).properties(
        height=360, autosize={"type": "fit-y", "contains": "padding"}
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
        .properties(height=320, autosize={"type": "fit-y", "contains": "padding"})
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


def append_row(ticker: str, ccy: str) -> None:
    """검색 결과를 거래 표(loaded_df)에 새 행으로 추가하고 에디터를 새로고침."""
    new = {"date": str(date.today()), "time": "09:30", "ticker": ticker,
           "side": "BUY", "price": None, "shares": None, "currency": ccy}
    df = st.session_state.get("loaded_df")
    if df is None or len(df) == 0:
        df = pd.DataFrame([new])
    else:
        df = pd.concat([df, pd.DataFrame([new])], ignore_index=True)
    st.session_state["loaded_df"] = df
    st.session_state["editor_ver"] = st.session_state.get("editor_ver", 0) + 1
    st.rerun()


def render_currency_section(ccy: str, ccy_txns: list[dict]) -> None:
    """한 통화(USD 또는 KRW)의 계좌·손익 섹션을 통째로 렌더. 단일통화라 기존 함수 그대로 사용."""
    st.header(CCY_LABEL.get(ccy, f"{ccy} 계좌"))

    results = jc.process_portfolio(ccy_txns)
    price_frames = {tk: prices.get_prices(tk) for tk in results}
    last_closes = {tk: prices.latest_close(price_frames[tk]) for tk in results}
    account = jc.current_account(ccy_txns, last_closes)

    missing = [tk for tk in results if last_closes[tk] is None]
    if missing:
        st.warning(f"시세를 못 받은 종목(평가 0 처리): {', '.join(missing)}")

    # 계좌 요약
    a = st.columns(3)
    a[0].metric("계좌가치", fmt_money(account["account_value"], ccy), help="현금 + 보유 시가평가")
    a[1].metric("현금잔고", fmt_money(account["cash"], ccy), help="입금·매도 − 출금·매수")
    a[2].metric("보유 시가", fmt_money(account["holdings_value"], ccy))

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

    if not results:
        st.info("이 통화의 매매 기록이 없습니다(현금 거래만).")
        return

    # 매매 손익
    st.subheader("매매 손익 — 종목별")
    summary = jc.portfolio_summary(results, last_closes)
    if ccy == "KRW" and "티커" in summary.columns:  # 한글 종목명 병기(표시용)
        summary.insert(1, "종목명", summary["티커"].map(lambda t: krx.resolve_name(t) or ""))
    m = st.columns(4)
    m[0].metric("실현손익 합계", fmt_money(summary["실현손익"].sum(), ccy))
    m[1].metric("미실현손익 합계", fmt_money(summary["미실현손익"].sum(), ccy))
    m[2].metric("총손익 합계", fmt_money(summary["총손익"].sum(), ccy))
    tot_cost = summary["매수금액"].sum()
    tot_ret = (summary["총손익"].sum() / tot_cost * 100.0) if tot_cost > 0 else 0.0
    m[3].metric("수익률", f"{tot_ret:+.2f}%", help="총손익 합계 / 누적 매수금액")

    money = "{:,.0f}" if ccy == "KRW" else "{:,.2f}"
    signed = "{:+,.0f}" if ccy == "KRW" else "{:+,.2f}"
    st.dataframe(
        summary.style.format({
            "보유수량": "{:g}", "평균단가": money, "실현손익": signed,
            "미실현손익": signed, "총손익": signed, "매수금액": money, "수익률%": "{:+.2f}",
        }),
        width="stretch",
    )

    curve = jc.portfolio_pnl_curve(results, price_frames)
    if not curve.empty:
        st.altair_chart(_line_with_zero(curve, f"누적손익 ({ccy})"), width="stretch")

    pick = st.selectbox("종목 선택", options=list(results), key=f"pick_{ccy}")
    res = results[pick]
    detail = jc.build_pnl_curve(res.rows, price_frames.get(pick))
    if not detail.empty:
        st.altair_chart(
            _line_with_zero(detail.rename(columns={"누적손익": pick}), f"누적손익 ({ccy})"),
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


# ---------- 시트 연결 ----------

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
        st.success("연결됨 — 시트의 거래를 불러왔습니다.")
    except Exception as exc:  # noqa: BLE001
        st.session_state["ws_ok"] = False
        st.error(f"연결 실패: {exc}")

if not st.session_state.get("ws_ok"):
    st.info("구글시트를 연결하면 매매 입력·저장이 활성화됩니다.")
    st.stop()


# ---------- CSV 가져오기 ----------

SCHEMA_COLS = ["date", "time", "ticker", "side", "price", "shares", "currency"]
TEMPLATE_CSV = (
    "date,time,ticker,side,price,shares,currency\n"
    "2026-06-01,09:00,,DEPOSIT,1000,,USD\n"
    "2026-06-01,09:30,AAPL,BUY,150,10,USD\n"
    "2026-06-02,14:00,AAPL,SELL,160,4,USD\n"
    "2026-06-03,09:05,,DEPOSIT,1000000,,KRW\n"
    "2026-06-03,09:30,005930.KS,BUY,70000,10,KRW\n"
)

with st.expander("📥 CSV로 한 번에 가져오기 (선택)", expanded=False):
    st.caption(
        "아래 양식 CSV를 받아 채운 뒤 업로드하세요. 컬럼: date,time,ticker,side,price,shares,currency. "
        "입금·출금은 ticker 비우고 side=DEPOSIT/WITHDRAW, price=금액, currency=USD/KRW. "
        "국내 주식은 005930.KS(코스피)/.KQ(코스닥) 코드."
    )
    st.download_button("양식 CSV 받기", TEMPLATE_CSV, file_name="trades_template.csv", mime="text/csv")
    up = st.file_uploader("CSV 업로드", type=["csv"], key="csv_up")
    if up is not None:
        try:
            imported = pd.read_csv(up, dtype=str)
            missing = [c for c in SCHEMA_COLS if c not in imported.columns]
            if missing:
                st.error(f"필수 컬럼 누락: {missing} — 양식 CSV를 사용하세요.")
            else:
                imported = imported[SCHEMA_COLS]
                st.dataframe(imported, width="stretch")
                if st.button("이 데이터로 채우기", key="csv_apply"):
                    st.session_state["loaded_df"] = imported
                    st.session_state["editor_ver"] = st.session_state.get("editor_ver", 0) + 1
                    st.success(f"{len(imported)}행을 불러왔습니다. 아래 표에서 확인 후 💾 저장하세요.")
                    st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"CSV 읽기 실패: {exc}")


# ---------- 종목 검색해서 추가 (국내) ----------

with st.expander("🔎 종목 검색해서 추가 (국내 주식 이름/코드)", expanded=False):
    st.caption("국내 주식은 .KS/.KQ를 몰라도 됩니다. 종목명(삼성전자) 또는 6자리 코드(005930)로 검색하세요.")
    q = st.text_input("종목명 또는 6자리 코드", key="krx_q", placeholder="예: 삼성전자, 005930")
    if q.strip():
        hits = krx.search(q)
        if hits:
            labels = [f"{h['name']} ({h['code']}, {h['market']}) → {h['ticker']}" for h in hits]
            idx = st.selectbox("후보 선택", range(len(hits)), format_func=lambda i: labels[i], key="krx_pick")
            if st.button("➕ 이 종목 추가", key="krx_add"):
                append_row(hits[idx]["ticker"], "KRW")
        else:
            st.caption("국내 목록에 없습니다. 미국 종목이면 아래 표에 티커(예: AAPL)를 직접 입력하세요.")


# ---------- 거래 입력 표 ----------

st.subheader("거래 입력")
st.caption(
    "매매(BUY/SELL)는 티커·가격·수량을. **입금·출금(DEPOSIT/WITHDRAW)은 티커 비우고 '가격/금액'에 금액만.** "
    "**통화**: 입금·출금은 직접 고르고, 매매는 티커로 자동(.KS/.KQ면 원화). "
    "**국내 주식은 005930.KS(코스피)/.KQ(코스닥) 코드.** 같은 날 단타는 시각으로 순서. 다 고친 뒤 **저장**."
)

loaded = st.session_state.get("loaded_df")
if loaded is None or loaded.empty:
    today = date.today()
    loaded = pd.DataFrame([
        {"date": str(today), "time": "09:00", "ticker": "", "side": "DEPOSIT", "price": 1000.0, "shares": None, "currency": "USD"},
        {"date": str(today), "time": "09:30", "ticker": "AAPL", "side": "BUY", "price": 150.0, "shares": 10.0, "currency": "USD"},
    ])

# 시트·CSV의 문자열을 에디터 컬럼 타입에 맞게 정리
edit_df = loaded.copy()
if "currency" not in edit_df.columns:
    edit_df["currency"] = "USD"
for c in ("ticker", "side", "time", "currency"):
    if c in edit_df.columns:
        edit_df[c] = edit_df[c].astype(str).replace({"nan": "", "None": ""})
edit_df["currency"] = edit_df["currency"].replace({"": "USD"}).str.upper()
edit_df["date"] = pd.to_datetime(edit_df["date"], errors="coerce")
edit_df["price"] = pd.to_numeric(edit_df["price"], errors="coerce")
edit_df["shares"] = pd.to_numeric(edit_df["shares"], errors="coerce")

editor_key = f"editor_{st.session_state.get('editor_ver', 0)}"
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
    },
)

if st.button("💾 저장", type="primary"):
    try:
        save_df = edited.copy()
        save_df["date"] = pd.to_datetime(save_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        ws = sheets.connect(st.session_state["sheet_url"])
        n = sheets.save_trades(ws, save_df)
        st.session_state["loaded_df"] = sheets.load_trades(ws)
        st.success(f"저장 완료 — {n}건 기록.")
    except Exception as exc:  # noqa: BLE001
        st.error(f"저장 실패: {exc}")


# ---------- 거래 → Txn 변환 (매매 + 현금) ----------

CASH_SIDES = {"DEPOSIT", "WITHDRAW"}
txns: list[dict] = []
for _, row in edited.iterrows():
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
    }
    if side in CASH_SIDES:
        txns.append({**base, "ticker": "", "side": side,
                     "price": float(price), "shares": 0.0, "currency": ccy})
    else:  # BUY / SELL — 티커·수량 필요 (통화는 티커로 자동 판정)
        if not tk or pd.isna(row.get("shares")):
            continue
        txns.append({**base, "ticker": tk, "side": side,
                     "price": float(price), "shares": float(row["shares"]), "currency": ccy})

if not txns:
    st.info("거래를 한 건 이상 입력하세요.")
    st.stop()


# ---------- 통화별 섹션 ----------

by_ccy = jc.split_by_currency(txns)
# USD 먼저, 그다음 KRW, 나머지는 알파벳 순
order = [c for c in ("USD", "KRW") if c in by_ccy] + sorted(c for c in by_ccy if c not in ("USD", "KRW"))
for i, ccy in enumerate(order):
    if i > 0:
        st.divider()
    render_currency_section(ccy, by_ccy[ccy])
