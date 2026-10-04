@echo off
:: SystemPanel - single entry: install/repair autostart + launch panel in background (no console)
:: Must be run as administrator for temps (CPU/disk) and to create Highest task. Handles UAC elevation.
chcp 65001 1>nul 2>&1
title SystemPanel

:: ============================================================
::  Configuration - these are the only two lines you need to edit
:: ============================================================
::  PYTHONW   : full path to pythonw.exe of the env where requirements.txt is installed
::  PANEL_DIR : folder containing this .bat (auto-detected, normally leave as-is)
:: ============================================================
set "PYTHONW=Z:\conda\envs\E1\pythonw.exe"
set "PANEL_DIR=%~dp0"
if "%PANEL_DIR:~-1%"=="\" set "PANEL_DIR=%PANEL_DIR:~0,-1%"

:: Self-elevate: if not admin, relaunch as admin and exit. The elevated instance does all work.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Not running as admin - requesting elevation...
    echo   Please click Yes on the UAC prompt.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%PANEL_DIR%'"
    exit /b
)

echo ========================================
echo    SystemPanel - Setup + Launch
echo    (running as admin - temps enabled)
echo ========================================
echo.

echo [1/3] Cleaning old panel processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*panel*' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host \"  Killed PID $($_.ProcessId)\" } catch {} }; Start-Sleep -Milliseconds 800; Write-Host '  Done.'"

echo.
echo [2/3] Cleaning legacy tasks and launchers...
powershell -NoProfile -Command "$tasks = @('SystemPanel','ResourcePanel'); foreach ($t in $tasks) { try { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop; Write-Host \"  Removed: $t\" } catch {} } Remove-Item -Path '%PANEL_DIR%\panel_launcher.ps1' -ErrorAction SilentlyContinue | Out-Null; Write-Host '  Done.'"

echo.
echo [3/3] Creating scheduled task (Highest / admin, pythonw - hidden)...
powershell -NoProfile -Command "$err=''; try { $a = New-ScheduledTaskAction -Execute '%PYTHONW%' -Argument '-m panel' -WorkingDirectory '%PANEL_DIR%'; $t = New-ScheduledTaskTrigger -AtLogon; $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest; $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden; Register-ScheduledTask -TaskName 'SystemPanel' -Action $a -Trigger $t -Principal $p -Settings $s -Description 'System Resource Monitor (admin, temps enabled, background)' | Out-Null; Enable-ScheduledTask -TaskName 'SystemPanel' | Out-Null; Write-Host '  Done. RunLevel=Highest (admin) + pythonw (no console).' } catch { $err=$_.Exception.Message; Write-Host \"  FAILED: $err\" -ForegroundColor Red; Write-Host '  Task was NOT created - check you clicked Yes on UAC.' -ForegroundColor Yellow; exit 1 }"

echo.
echo ========================================
echo   Auto-start enabled (admin, background).
echo   Panel will launch on next login without console.
echo ========================================
echo.
echo Launching panel now in background (admin - temps will show)...
echo   Move it, then click elsewhere to lock and save position.
echo   This window will close automatically.
powershell -NoProfile -Command "Start-Process '%PYTHONW%' -ArgumentList '-m panel' -WorkingDirectory '%PANEL_DIR%' -WindowStyle Hidden | Out-Null; Write-Host '  Panel launched in background (pythonw).'"
echo   Done. Verifying...
powershell -NoProfile -Command "Start-Sleep -Seconds 1; Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*panel*' } | ForEach-Object { Write-Host (\"  Running: PID {0} {1}\" -f $_.ProcessId,$_.Name) }; if (-not (Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*panel*' })) { Write-Host '  WARNING: panel did not appear - check pythonw path and WorkingDirectory.' -ForegroundColor Yellow }"
timeout /t 3 >nul
exit /b
