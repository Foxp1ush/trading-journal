# 로컬 미리보기 — 전용 .venv로 매매일지를 8510 포트에 실행.
# 사용: 이 폴더에서  .\run_local.ps1
# 종료: 터미널에서 Ctrl+C
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host ".venv가 없습니다. 먼저 만드세요:" -ForegroundColor Yellow
    Write-Host '  python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    exit 1
}
Write-Host "http://localhost:8510 에서 열립니다 (Ctrl+C로 종료)" -ForegroundColor Cyan
& $py -m streamlit run (Join-Path $PSScriptRoot "journal_app.py") --server.port 8510
