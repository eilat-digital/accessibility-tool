@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo.
echo  =====================================================
echo   התקנת שירות Windows — מערכת הנגשת מסמכים
echo   עיריית אילת
echo  =====================================================
echo.

:: ── בדיקת הרשאות מנהל ────────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [שגיאה] יש להפעיל כמנהל מערכת ^(Run as Administrator^)
    pause
    exit /b 1
)

:: ── הגדרת נתיבים ──────────────────────────────────────────────────────────
set "APP_DIR=%~dp0"
:: הסר backslash אחרון אם קיים
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "SERVICE_NAME=PDFAccessibility"
set "SERVICE_DISPLAY=הנגשת מסמכים - עיריית אילת"
set "NSSM_DIR=%APP_DIR%\tools\nssm"
set "NSSM_EXE=%NSSM_DIR%\nssm.exe"
set "PYTHON_EXE="

:: ── מציאת Python ──────────────────────────────────────────────────────────
for %%P in (
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
) do (
    if exist %%P (
        set "PYTHON_EXE=%%~P"
        goto :found_python
    )
)
:: fallback: python ב-PATH
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%i in ('where python') do (
        set "PYTHON_EXE=%%i"
        goto :found_python
    )
)
echo  [שגיאה] Python לא נמצא. התקן Python 3.12 ונסה שוב.
pause & exit /b 1

:found_python
echo  [OK] Python: %PYTHON_EXE%

:: ── בדיקת waitress ────────────────────────────────────────────────────────
"%PYTHON_EXE%" -c "import waitress" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [מתקין] waitress...
    "%PYTHON_EXE%" -m pip install waitress --quiet
    if %errorlevel% neq 0 (
        echo  [שגיאה] לא הצלחתי להתקין waitress
        pause & exit /b 1
    )
)
echo  [OK] waitress מותקן

:: ── הורדת NSSM אם חסר ────────────────────────────────────────────────────
if not exist "%NSSM_EXE%" (
    echo  [מוריד] NSSM ^(Non-Sucking Service Manager^)...
    mkdir "%NSSM_DIR%" 2>nul
    powershell -Command ^
        "Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile '%NSSM_DIR%\nssm.zip' -UseBasicParsing" >nul 2>&1
    if not exist "%NSSM_DIR%\nssm.zip" (
        echo  [שגיאה] לא הצלחתי להוריד NSSM. הורד ידנית מ: https://nssm.cc
        echo           חלץ את nssm.exe ל: %NSSM_DIR%\nssm.exe
        pause & exit /b 1
    )
    powershell -Command ^
        "Expand-Archive '%NSSM_DIR%\nssm.zip' -DestinationPath '%NSSM_DIR%\extracted' -Force" >nul 2>&1
    :: מציאת nssm.exe (64-bit)
    for /r "%NSSM_DIR%\extracted" %%f in (nssm.exe) do (
        echo %%f | find /i "win64" >nul && copy "%%f" "%NSSM_EXE%" >nul
    )
    if not exist "%NSSM_EXE%" (
        for /r "%NSSM_DIR%\extracted" %%f in (nssm.exe) do copy "%%f" "%NSSM_EXE%" >nul
    )
    del "%NSSM_DIR%\nssm.zip" >nul 2>&1
)
echo  [OK] NSSM: %NSSM_EXE%

:: ── הסרת שירות קיים ──────────────────────────────────────────────────────
"%NSSM_EXE%" status "%SERVICE_NAME%" >nul 2>&1
if %errorlevel%==0 (
    echo  [עדכון] מסיר גרסה קודמת של השירות...
    "%NSSM_EXE%" stop "%SERVICE_NAME%" >nul 2>&1
    "%NSSM_EXE%" remove "%SERVICE_NAME%" confirm >nul 2>&1
)

:: ── יצירת השירות ──────────────────────────────────────────────────────────
echo  [מתקין] מתקין שירות Windows...

"%NSSM_EXE%" install "%SERVICE_NAME%" "%PYTHON_EXE%" "run_server.py"
"%NSSM_EXE%" set "%SERVICE_NAME%" DisplayName "%SERVICE_DISPLAY%"
"%NSSM_EXE%" set "%SERVICE_NAME%" Description "מערכת הנגשת מסמכים PDF/UA לעיריית אילת"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppDirectory "%APP_DIR%"
"%NSSM_EXE%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START

:: לוגים
set "LOG_DIR=%APP_DIR%\logs"
mkdir "%LOG_DIR%" 2>nul
"%NSSM_EXE%" set "%SERVICE_NAME%" AppStdout "%LOG_DIR%\service_stdout.log"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppStderr "%LOG_DIR%\service_stderr.log"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppRotateFiles 1
"%NSSM_EXE%" set "%SERVICE_NAME%" AppRotateOnline 1
"%NSSM_EXE%" set "%SERVICE_NAME%" AppRotateBytes 5242880

:: הפעלה מחדש אוטומטית בכשל
"%NSSM_EXE%" set "%SERVICE_NAME%" AppExit Default Restart
"%NSSM_EXE%" set "%SERVICE_NAME%" AppRestartDelay 5000

:: ── הפעלת השירות ──────────────────────────────────────────────────────────
echo  [מפעיל] מפעיל את השירות...
"%NSSM_EXE%" start "%SERVICE_NAME%"
timeout /t 3 /nobreak >nul

"%NSSM_EXE%" status "%SERVICE_NAME%"

echo.
echo  =====================================================
echo   ✓ השירות הותקן והופעל בהצלחה!
echo.
echo   גישה למערכת:
echo   http://localhost:5001
echo   http://[IP-השרת]:5001
echo.
echo   ניהול השירות:
echo   services.msc  → הנגשת מסמכים - עיריית אילת
echo  =====================================================
echo.
pause
