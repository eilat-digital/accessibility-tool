@echo off
chcp 65001 >nul
echo.
echo  הסרת שירות הנגשת מסמכים...
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [שגיאה] יש להפעיל כמנהל מערכת
    pause & exit /b 1
)
set "NSSM_EXE=%~dp0tools\nssm\nssm.exe"
"%NSSM_EXE%" stop PDFAccessibility
"%NSSM_EXE%" remove PDFAccessibility confirm
echo  [OK] השירות הוסר.
pause
