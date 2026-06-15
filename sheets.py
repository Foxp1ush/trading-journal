"""구글시트 I/O — 서비스 계정으로 사용자 본인 시트를 읽고 쓴다(gspread).

사용자는 자기 소유 시트를 서비스 계정 이메일에 '편집' 공유한 뒤 URL을 앱에 붙여넣는다.
비공개·영속성은 사용자 본인 구글 계정이 책임(앱은 공유된 시트에만 접근).

거래 컬럼: date, time, ticker, side, price, shares (워크시트명 'trades').
"""

from __future__ import annotations

import json
from pathlib import Path

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

WORKSHEET = "trades"
HEADER = ["date", "time", "ticker", "side", "price", "shares", "currency"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# 로컬 개발 편의: 다운로드한 서비스계정 JSON을 이 경로에 두면 secrets.toml 없이도 동작.
# (.gitignore에 포함되어 커밋되지 않음. 클라우드에서는 st.secrets를 사용.)
LOCAL_SA_JSON = Path(__file__).resolve().parent / "service_account.json"


def _sa_info() -> dict:
    """서비스 계정 정보 dict 반환. ① st.secrets ② 로컬 JSON 파일 순으로 탐색."""
    try:
        if "gcp_service_account" in st.secrets:
            return dict(st.secrets["gcp_service_account"])
    except Exception:  # noqa: BLE001 — secrets 파일 자체가 없으면 in 접근이 예외
        pass
    if LOCAL_SA_JSON.exists():
        return json.loads(LOCAL_SA_JSON.read_text(encoding="utf-8"))
    raise RuntimeError(
        "서비스 계정 자격증명이 없습니다. "
        "클라우드: Secrets에 [gcp_service_account] 입력 / "
        "로컬: service_account.json 파일을 폴더에 두세요."
    )


@st.cache_resource(show_spinner=False)
def _client() -> gspread.Client:
    """서비스 계정으로 gspread 클라이언트 생성(세션 1회)."""
    creds = Credentials.from_service_account_info(_sa_info(), scopes=SCOPES)
    return gspread.authorize(creds)


def service_account_email() -> str:
    """사용자에게 '이 이메일에 시트를 공유하라'고 안내할 주소."""
    try:
        return str(_sa_info().get("client_email", "(client_email 없음)"))
    except Exception:  # noqa: BLE001
        return "(자격증명 미설정 — 운영자가 서비스 계정을 등록해야 함)"


def connect(sheet_url: str) -> gspread.Worksheet:
    """시트 URL로 워크시트 'trades' 반환(없으면 헤더와 함께 생성)."""
    sh = _client().open_by_url(sheet_url.strip())
    try:
        ws = sh.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET, rows=200, cols=len(HEADER))
        ws.update([HEADER], "A1")
    # 헤더 보정(빈 시트 대비)
    if not ws.row_values(1):
        ws.update([HEADER], "A1")
    return ws


def load_trades(ws: gspread.Worksheet) -> pd.DataFrame:
    """워크시트를 DataFrame으로(컬럼 = HEADER). 숫자 형변환 + 하위호환 백필.

    기존 6열 시트(currency 없음)도 읽히도록 실제 헤더 기준으로 읽고 HEADER로 reindex,
    누락된 currency는 USD로 채운다.
    """
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=HEADER)
    header = [h.strip() for h in values[0]]
    body = values[1:]
    df = pd.DataFrame(body, columns=header) if body else pd.DataFrame(columns=header)

    for col in HEADER:               # 누락 컬럼 보정(특히 currency)
        if col not in df.columns:
            df[col] = ""
    df = df[HEADER]

    df["currency"] = (
        df["currency"].astype(str).str.strip()
        .replace({"": "USD", "nan": "USD", "None": "USD"}).str.upper()
    )
    for col in ("price", "shares"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("date", "time", "ticker", "side"):
        df[col] = df[col].astype(str).replace({"nan": "", "None": ""})
    return df


def save_trades(ws: gspread.Worksheet, df: pd.DataFrame) -> int:
    """DataFrame을 시트에 통째로 덮어쓰기(clear + update). 저장 행수 반환."""
    out = df.copy()
    for col in HEADER:
        if col not in out.columns:
            out[col] = ""
    out = out[HEADER]
    # 완전히 빈 행 제거(티커·가격·수량 모두 비면 스킵)
    def _empty(row) -> bool:
        return (
            not str(row["ticker"]).strip()
            and pd.isna(row["price"]) and pd.isna(row["shares"])
        )
    out = out[~out.apply(_empty, axis=1)]
    out = out.fillna("")
    values = [HEADER] + out.astype(object).values.tolist()
    ws.clear()
    ws.update(values, "A1")
    return len(out)
