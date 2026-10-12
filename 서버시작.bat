@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo =============================
echo   톡톡 라이브 퀴즈 서버 시작
 echo =============================
echo.
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py app.py
) else (
    python app.py
)
echo.
echo 서버가 종료되었습니다. 아무 키나 누르면 창이 닫힙니다.
pause >nul
