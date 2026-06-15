# 📒 주식 매매일지 (Trading Journal)

여러 종목의 실제 매매를 기록하면 **종목별 평균단가·실현/미실현 손익·누적손익 곡선**을 보여주는
웹 앱입니다. Streamlit으로 만들고 **무료 공개 URL**로 배포하며, 데이터는 **각자 본인 구글시트**에
저장합니다(비공개·영속성은 본인 구글 계정이 책임).

- 체결가는 직접 입력(야후 무료 데이터엔 분 단위 체결가가 없음).
- 보유 중인 물량은 야후 일별 종가로 평가(하이브리드). 단타(당일 청산)면 종가 평가는 자동 비활성.
- 원가는 **평균단가(평단)** 방식 — 국내 증권사 표시와 동일.

---

## 🙋 사용하는 사람(친구)용 — 3단계

1. 본인 구글 드라이브에서 **빈 구글시트**를 하나 만듭니다.
2. 그 시트를 앱 상단에 표시된 **서비스 계정 이메일**에 **편집** 권한으로 공유합니다.
3. 시트 **URL을 복사**해 앱에 붙여넣고 **연결**을 누릅니다.

이후 표에 매매를 입력하고 **💾 저장**을 누르면 본인 시트에 기록됩니다. 다음에 다시 들어와
연결하면 그대로 불러옵니다. (PC/노트북 브라우저 권장)

---

## 🛠 배포하는 사람(운영자)용 — 최초 1회 세팅

### A. 구글 서비스 계정 만들기
1. [Google Cloud Console](https://console.cloud.google.com) → 프로젝트 생성.
2. **API 및 서비스 → 라이브러리**에서 **Google Sheets API**와 **Google Drive API** 사용 설정.
3. **사용자 인증 정보 → 서비스 계정 만들기** → 생성된 계정에서 **키 → JSON 키 추가** → JSON 다운로드.
4. JSON 안의 `client_email`(예: `...@...iam.gserviceaccount.com`)이 사용자가 시트를 공유할 주소입니다.

### B. 비밀 설정
- **로컬 실행**: `.streamlit/secrets.toml.example`을 `secrets.toml`로 복사 후 JSON 값으로 채웁니다.
  (`secrets.toml`은 `.gitignore`에 있어 커밋되지 않습니다.)
- **클라우드**: 배포 후 앱 **Settings → Secrets**에 같은 내용을 붙여넣습니다.

### C. 배포 (Streamlit Community Cloud — 무료)
1. 이 폴더를 **공개 GitHub 저장소**로 push. (매매 데이터는 저장소가 아니라 각자 시트에 있으므로
   공개되어도 안전합니다.)
2. [share.streamlit.io](https://share.streamlit.io) → **New app** → 저장소·브랜치 선택,
   **Main file path = `journal_app.py`**.
3. **Advanced settings → Secrets**에 위 B의 내용을 입력 → Deploy.
4. 생성된 공개 URL(`https://<앱이름>.streamlit.app`)을 친구에게 전달.

> 무료 플랜은 유휴 시 잠들었다가 첫 접속에 수십 초 콜드스타트가 있을 수 있습니다.

---

## 개선 & 업데이트 워크플로

클라우드 앱은 **실시간 반영이 아니라** GitHub `master`에 **push하면 ~1분 뒤 자동 재배포**됩니다.
⚠️ `master` = 친구가 쓰는 **라이브** 앱이므로, 깨진 코드를 올리면 친구 화면도 바로 깨집니다.
반드시 **로컬에서 확인하고 점검을 통과한 뒤** push하세요.

### 최초 1회 — 전용 가상환경 만들기

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 매번 — 개선 루프

```powershell
# 1) 코드 수정 (journal_app.py 등)

.\run_local.ps1     # 2) 로컬 미리보기 → http://localhost:8510 에서 눈으로 확인 (Ctrl+C 종료)

.\check.ps1         # 3) 자동 점검 (import·평단 검산·앱 부팅). "모든 점검 통과"여야 함

# 4) 통과하면 push → 클라우드 자동 재배포
git add -A
git commit -m "개선 내용 설명"
git push
```

- **Secrets·친구 시트 공유는 그대로 유지**됩니다(코드만 갱신). 키·시트는 다시 안 건드려도 됨.
- **의존성 추가**: `requirements.txt`에 추가 → `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
  → 로컬 확인 → push(클라우드가 자동 재설치).
- **Cloud에서만 빌드 실패**하면 대개 Python 버전/휠 문제 — `requirements.txt` 버전 핀을 낮추거나
  Streamlit Cloud 앱 설정의 Python 버전을 조정하세요(로컬 venv는 3.13, Cloud와 다를 수 있음).

> `.\check.ps1`은 시트 실연결·시세 호출 없이 수 초 만에 도는 **사전 게이트**입니다.
> 시트 저장까지 실제로 확인하려면 `.\run_local.ps1`로 띄워 직접 연결·저장해 보세요(`.streamlit/secrets.toml` 필요).

## 구조

| 파일 | 역할 |
|---|---|
| `journal_app.py` | Streamlit UI (시트 연결 · 입력표 · 저장 · 요약 · 곡선) |
| `journal_core.py` | 평균단가 엔진 + 여러 종목 집계 + 누적손익 곡선 (순수 로직) |
| `prices.py` | yfinance 시세 + 세션 캐시 |
| `sheets.py` | gspread 서비스 계정으로 사용자 시트 read/write |
| `smoke_test.py` | push 전 자동 점검(import·평단 검산·앱 부팅) |
| `run_local.ps1` / `check.ps1` | 로컬 실행 / 점검 편의 스크립트 |

## 알려진 한계

- 서비스 계정이 공유된 시트를 읽을 수 있어 **운영자로부터 완전 격리는 아님**(친구 상대 비공개).
  완전 격리가 필요하면 사용자별 OAuth 로그인 필요.
- 수동 체결가(원가) vs 야후 조정종가 혼용 → 장기 보유 중 분할/배당이 끼면 미실현 평가 미세 오차.
- 수익률 = 단순 money-on-money(총손익 ÷ 누적 매수금액). TWR/IRR은 추후.
- yfinance 시세는 일부 비미국 티커를 지원하지 않을 수 있습니다.

투자 자문이 아니며, 수수료·세금은 반영하지 않습니다.
