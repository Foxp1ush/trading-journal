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
import prices
import sheets

st.set_page_config(page_title="주식 매매일지", page_icon="📒", layout="wide")
st.title("📒 주식 매매일지")
st.caption(
    "실제 체결가를 직접 입력 → 종목별 평균단가·실현손익, 보유분은 일별 종가로 평가(하이브리드). "
    "여러 종목 기록 가능. 수수료·세금 미반영, 투자 자문 아님."
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


# ---------- 거래 입력 표 ----------

st.subheader("거래 입력")
st.caption("같은 날 여러 번 매매(단타)는 시각(HH:MM)으로 순서가 정해집니다. 다 고친 뒤 **저장**을 누르세요.")

loaded = st.session_state.get("loaded_df")
if loaded is None or loaded.empty:
    today = date.today()
    loaded = pd.DataFrame([
        {"date": str(today), "time": "09:30", "ticker": "AAPL", "side": "BUY", "price": 150.0, "shares": 100.0},
    ])

# 시트의 문자열 날짜를 DateColumn용 datetime으로
edit_df = loaded.copy()
edit_df["date"] = pd.to_datetime(edit_df["date"], errors="coerce")

edited = st.data_editor(
    edit_df, num_rows="dynamic", width="stretch", key="editor",
    column_config={
        "date": st.column_config.DateColumn("날짜", format="YYYY-MM-DD", required=True),
        "time": st.column_config.TextColumn("시각", help="HH:MM (24시간)", default="09:30"),
        "ticker": st.column_config.TextColumn("티커", required=True),
        "side": st.column_config.SelectboxColumn("구분", options=["BUY", "SELL"], required=True, default="BUY"),
        "price": st.column_config.NumberColumn("가격(체결가)", min_value=0.0, format="%.4f", required=True),
        "shares": st.column_config.NumberColumn("수량(주)", min_value=0.0, format="%g", required=True),
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


# ---------- 거래 → Txn 변환 ----------

txns: list[dict] = []
for _, row in edited.iterrows():
    d = row.get("date")
    tk = str(row.get("ticker") or "").strip().upper()
    if pd.isna(d) or not tk or pd.isna(row.get("price")) or pd.isna(row.get("shares")):
        continue
    txns.append({
        "date": pd.Timestamp(d).strftime("%Y-%m-%d"),
        "time": str(row.get("time") or "09:30"),
        "ticker": tk,
        "side": str(row.get("side") or "BUY"),
        "price": float(row["price"]),
        "shares": float(row["shares"]),
    })

if not txns:
    st.info("거래를 한 건 이상 입력하세요.")
    st.stop()


# ---------- 계산 ----------

results = jc.process_portfolio(txns)
price_frames = {tk: prices.get_prices(tk) for tk in results}
last_closes = {tk: prices.latest_close(price_frames[tk]) for tk in results}

summary = jc.portfolio_summary(results, last_closes)

# 합계 metric
st.subheader("포트폴리오 요약")
tot_realized = summary["실현손익"].sum()
tot_unreal = summary["미실현손익"].sum()
tot_total = summary["총손익"].sum()
tot_cost = summary["매수금액"].sum()
tot_ret = (tot_total / tot_cost * 100.0) if tot_cost > 0 else 0.0

m = st.columns(4)
m[0].metric("실현손익 합계", f"{tot_realized:+,.2f}")
m[1].metric("미실현손익 합계", f"{tot_unreal:+,.2f}")
m[2].metric("총손익 합계", f"{tot_total:+,.2f}")
m[3].metric("수익률", f"{tot_ret:+.2f}%", help="총손익 합계 / 누적 매수금액")

st.dataframe(
    summary.style.format({
        "보유수량": "{:g}", "평균단가": "{:,.4f}", "실현손익": "{:+,.2f}",
        "미실현손익": "{:+,.2f}", "총손익": "{:+,.2f}", "매수금액": "{:,.2f}",
        "수익률%": "{:+.2f}",
    }),
    width="stretch",
)

# 종목 중 종가를 못 받은 경우 안내
missing = [tk for tk in results if last_closes[tk] is None]
if missing:
    st.warning(f"시세를 못 받은 종목(미실현 0 처리): {', '.join(missing)}")

# 전체 누적손익 곡선
st.subheader("전체 누적손익 곡선")
curve = jc.portfolio_pnl_curve(results, price_frames)
if not curve.empty:
    st.altair_chart(_line_with_zero(curve, "누적손익 (가격단위)"), width="stretch")

# 종목별 상세
st.subheader("종목별 상세")
pick = st.selectbox("종목 선택", options=list(results))
res = results[pick]
detail = jc.build_pnl_curve(res.rows, price_frames.get(pick))
if not detail.empty:
    st.altair_chart(
        _line_with_zero(detail.rename(columns={"누적손익": pick}), "누적손익 (가격단위)"),
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
