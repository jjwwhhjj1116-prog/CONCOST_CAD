@echo off
chcp 65001 >nul
title CONCOST CAD 서버
cd /d "%~dp0"

echo ============================================
echo   CONCOST CAD - 내부 마감 적산 자동화
echo --------------------------------------------
echo   서버 시작 중... 잠시만 기다리세요.
echo   주소: http://127.0.0.1:8765
echo   (이 검은 창을 닫으면 서버가 종료됩니다)
echo ============================================
echo.

REM 3초 뒤 브라우저 자동 열기 (서버 뜰 시간 확보)
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8765"

REM 서버 실행 (py 없으면 python 시도)
py app.py
if errorlevel 1 (
  echo.
  echo [py 실행 실패] python 으로 재시도합니다...
  python app.py
)

echo.
echo 서버가 종료되었습니다.
pause
