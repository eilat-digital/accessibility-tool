@echo off
chcp 1255 >nul
setlocal EnableDelayedExpansion

echo.
echo  =====================================================
echo   התקנת שירות ngrok
echo   עיריית אילת - מערכת הנגשת מסמכים
echo  =====================================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [שגיאה] הפעל כמנהל מערכת - Run as Administrator
    pause
    exit /b 1
)

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "NSSM_EXE=%APP_DIR%\tools\nssm\nssm.exe"
set "NGROK_DOMAIN=platform-synopses-canola.ngrok-free.dev"
set "NGROK_SERVICE=NgrokTunnel"
set "NGROK_EXE="

for /f "tokens=*" %%i in ('where ngrok 2^>nul') do (
    set "NGROK_EXE=%%i"
    goto :found_ngrok
)
echo  [שגיאה] ngrok לא נמצא
pause & exit /b 1

:found_ngrok
echo  [OK] ngrok: %NGROK_EXE%

if not exist "%NSSM_EXE%" (
    echo  [שגיאה] NSSM לא נמצא - הרץ תחילה install_service.bat
    pause & exit /b 1
)

"%NSSM_EXE%" status "%NGROK_SERVICE%" >nul 2>&1
if %errorlevel%==0 (
    "%NSSM_EXE%" stop "%NGROK_SERVICE%" >nul 2>&1
    "%NSSM_EXE%" remove "%NGROK_SERVICE%" confirm >nul 2>&1
)

"%NSSM_EXE%" install "%NGROK_SERVICE%" "%NGROK_EXE%" "http --domain=%NGROK_DOMAIN% 5001"
"%NSSM_EXE%" set "%NGROK_SERVICE%" DisplayName "ngrok Tunnel - Eilat Accessibility"
"%NSSM_EXE%" set "%NGROK_SERVICE%" Start SERVICE_AUTO_START
"%NSSM_EXE%" set "%NGROK_SERVICE%" AppExit Default Restart
"%NSSM_EXE%" set "%NGROK_SERVICE%" AppRestartDelay 10000

set "LOG_DIR=%APP_DIR%\logs"
mkdir "%LOG_DIR%" 2>nul
"%NSSM_EXE%" set "%NGROK_SERVICE%" AppStdout "%LOG_DIR%\ngrok_stdout.log"
"%NSSM_EXE%" set "%NGROK_SERVICE%" AppStderr "%LOG_DIR%\ngrok_stderr.log"

"%NSSM_EXE%" start "%NGROK_SERVICE%"
timeout /t 4 /nobreak >nul
"%NSSM_EXE%" status "%NGROK_SERVICE%"

echo.
echo  =====================================================
echo   השירות הותקן!
echo   כתובת גישה: https://%NGROK_DOMAIN%
echo  =====================================================
echo.
pause
