"""종목 검색 — 국내(이름/6자리 코드)·미국(이름/심볼) → 야후 파이낸스 티커.

FinanceDataReader 상장목록으로 검색·변환만 한다(시세는 yfinance):
- 국내: KRX 목록(Code·Name·Market) → .KS/.KQ, 통화 KRW.
- 미국: NASDAQ·NYSE·AMEX 목록(Symbol·Name) → 심볼 그대로, 통화 USD.
네트워크/라이브러리 실패 시 빈 결과로 폴백 → 앱은 티커 직접 입력으로 계속 동작.
"""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

_CODE_RE = re.compile(r"^\d{6}$")


@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_listing() -> pd.DataFrame:
    """KRX 상장목록 → 컬럼 [Code, Name, Market]. 실패 시 빈 DataFrame."""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
    except Exception:  # noqa: BLE001 — 네트워크/소스 변경 등은 빈 목록으로 흡수
        return pd.DataFrame(columns=["Code", "Name", "Market"])
    try:
        return pd.DataFrame({
            "Code": df["Code"].astype(str).str.zfill(6),
            "Name": df["Name"].astype(str),
            "Market": df["Market"].astype(str),
        })
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["Code", "Name", "Market"])


@st.cache_data(ttl=86400, show_spinner=False)
def load_us_listing() -> pd.DataFrame:
    """미국 상장목록(NASDAQ·NYSE·AMEX) → 컬럼 [Symbol, Name, Market]. 실패 시 빈 DataFrame."""
    frames = []
    try:
        import FinanceDataReader as fdr
        for mkt in ("NASDAQ", "NYSE", "AMEX"):
            try:
                d = fdr.StockListing(mkt)
                frames.append(pd.DataFrame({
                    "Symbol": d["Symbol"].astype(str),
                    "Name": d["Name"].astype(str),
                    "Market": mkt,
                }))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["Symbol", "Name", "Market"])
    if not frames:
        return pd.DataFrame(columns=["Symbol", "Name", "Market"])
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset="Symbol").reset_index(drop=True)


def to_yf_ticker(code: str, market: str) -> str:
    """6자리 코드 + 시장 → 야후 티커. KOSPI→.KS, 그 외(코스닥/코넥스)→.KQ."""
    code = str(code).strip().zfill(6)
    suffix = ".KS" if str(market).upper().startswith("KOSPI") else ".KQ"
    return f"{code}{suffix}"


def _search_kr(q: str, limit: int) -> list[dict]:
    df = load_krx_listing()
    if df.empty:
        return []
    if _CODE_RE.match(q):
        hit = df[df["Code"] == q]
    else:
        hit = df[df["Name"].str.lower().str.contains(q.lower(), na=False)]
    return [
        {"ticker": to_yf_ticker(r["Code"], r["Market"]), "name": r["Name"],
         "market": r["Market"], "currency": "KRW"}
        for _, r in hit.head(limit).iterrows()
    ]


def _search_us(q: str, limit: int) -> list[dict]:
    df = load_us_listing()
    if df.empty:
        return []
    ql = q.lower()
    by_symbol = df["Symbol"].str.upper() == q.upper()
    by_name = df["Name"].str.lower().str.contains(ql, na=False)
    hit = df[by_symbol | by_name]
    # 심볼 정확매치를 위로
    hit = hit.assign(_exact=by_symbol[hit.index].astype(int)).sort_values("_exact", ascending=False)
    return [
        {"ticker": str(r["Symbol"]).upper(), "name": r["Name"],
         "market": r["Market"], "currency": "USD"}
        for _, r in hit.head(limit).iterrows()
    ]


def search(query: str, limit: int = 12) -> list[dict]:
    """국내·미국 통합 검색. 반환: [{ticker, name, market, currency}, ...].

    6자리 숫자 → 국내 코드. 그 외엔 국내 종목명 + 미국 심볼/이름 모두 검색.
    """
    q = str(query).strip()
    if not q:
        return []
    if _CODE_RE.match(q):
        return _search_kr(q, limit)
    per = max(3, limit // 2)
    return (_search_kr(q, per) + _search_us(q, per))[:limit]


def resolve_name(yf_ticker: str) -> str | None:
    """야후 티커(.KS/.KQ)의 한글 종목명. 국내가 아니거나 못 찾으면 None."""
    t = str(yf_ticker).strip().upper()
    if not (t.endswith(".KS") or t.endswith(".KQ")):
        return None
    code = t.rsplit(".", 1)[0].zfill(6)
    df = load_krx_listing()
    if df.empty:
        return None
    hit = df[df["Code"] == code]
    return str(hit.iloc[0]["Name"]) if not hit.empty else None


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("to_yf_ticker:", to_yf_ticker("005930", "KOSPI"), to_yf_ticker("277810", "KOSDAQ"))
    print("search('삼성전자'):", search("삼성전자", 3))
    print("search('005930'):", search("005930"))
    print("search('apple'):", search("apple", 3))
    print("search('NVDA'):", search("NVDA", 3))
    print("resolve_name('005930.KS'):", resolve_name("005930.KS"))
