@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-validation.ps1"
set "AIK_HANDOFF_EXIT=%ERRORLEVEL%"
echo.
echo Validation exit code: %AIK_HANDOFF_EXIT%
echo The evidence path is printed above. No administrator elevation was requested.
pause
exit /b %AIK_HANDOFF_EXIT%
