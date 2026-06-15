"""시세 로더 — yfinance 일별 가격 + Streamlit 세션 캐시.

클라우드(Streamlit Cloud)는 영구 디스크가 없어 parquet 캐시를 두지 않고,
@st.cache_data 로 세션 내 재다운로드만 막는다. Close는 auto_adjust=True(배당·분할 조정).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
import yfinance as yf

DEFAULT_PERIOD_YEARS = 2


@st.cache_data(ttl=3600, show_spinner=False)
def get_prices(ticker: str, years: int = DEFAULT_PERIOD_YEARS) -> pd.DataFrame:
    """종목 일별 가격 DataFrame. 인덱스 tz-naive DatetimeIndex, 'Close'는 조정종가.

    실패/빈 응답이면 빈 DataFrame을 반환(앱은 종가 평가 없이 거래 이벤트 점만 사용).
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()
    try:
        df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    except Exception:  # noqa: BLE001 — 네트워크/티커 오류는 빈 프레임으로 흡수
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "Date"
    return df


def latest_close(price_frame: pd.DataFrame | None) -> float | None:
    """최신 종가(미실현 평가용). 데이터 없으면 None."""
    if price_frame is None or price_frame.empty:
        return None
    return float(price_frame["Close"].iloc[-1])
