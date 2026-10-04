@echo off
chcp 65001 1>nul 2>&1
title SystemPanel Uninstall

set "PANEL_DIR=%~dp0"
if "%PANEL_DIR:~-1%"=="\" set "PANEL_DIR=%PANEL_DIR:~0,-1%"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting admin rights...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo ========================================
echo    SystemPanel Uninstall
echo ========================================
echo.

echo [1/2] Stopping panel...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*panel*' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host \"  Stopped PID $($_.ProcessId)\" } catch {} }; Write-Host '  Done.'"

echo.
echo [2/2] Removing scheduled tasks...
powershell -NoProfile -Command ^
    "$tasks = @('SystemPanel','ResourcePanel','\u8D44\u6E90\u9762\u677F');" ^
    "foreach ($t in $tasks) {" ^
    "  try { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue | Out-Null;" ^
    "        Unregister-ScheduledTask -TaskName $t -Confirm:$^false -ErrorAction Stop;" ^
    "        Write-Host \"  Removed: $t\" } catch {}" ^
    "}" ^
    "Remove-Item -Path '%PANEL_DIR%\panel_launcher.ps1' -ErrorAction SilentlyContinue | Out-Null;" ^
    "Write-Host '  Done.'"

echo.
echo ========================================
echo   Uninstall complete.
echo   Auto-start disabled.
echo ========================================
echo.
pause
