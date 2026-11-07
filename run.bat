@echo off
chcp 65001 >nul
rem myAISky 백엔드 서버 실행 스크립트 (프로젝트 루트 기준으로 동작)
cd /d "%~dp0"

echo [myAISky] 백엔드 서버를 시작합니다: http://127.0.0.1:8000
echo [myAISky] 종료하려면 Ctrl+C 를 누르세요.
echo.

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

if errorlevel 1 (
    echo.
    echo [myAISky] 서버 실행에 실패했습니다. requirements.txt 설치 여부를 확인하세요:
    echo           python -m pip install -r requirements.txt
    pause
)
