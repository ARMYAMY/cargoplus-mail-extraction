@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\single_host.ps1" -Action Deploy
set "CARGOPLUS_EXIT=%ERRORLEVEL%"
if not "%CARGOPLUS_EXIT%"=="0" (
  echo.
  echo Deployment failed. Review the error above.
) else (
  echo.
  echo Deployment succeeded. You may close this window.
)
pause
exit /b %CARGOPLUS_EXIT%
