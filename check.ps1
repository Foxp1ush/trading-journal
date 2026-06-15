# push 전 자동 점검 — 전용 .venv로 smoke_test 실행.
# 사용: 이 폴더에서  .\check.ps1   (통과해야 push)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host ".venv가 없습니다. 먼저 만드세요:" -ForegroundColor Yellow
    Write-Host '  python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    exit 1
}
& $py (Join-Path $PSScriptRoot "smoke_test.py")
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n→ 점검 통과. 이제 push 해도 됩니다:" -ForegroundColor Green
    Write-Host '  git add -A; git commit -m "설명"; git push'
} else {
    Write-Host "`n→ 점검 실패. push 하지 말고 고치세요." -ForegroundColor Red
}
exit $LASTEXITCODE
