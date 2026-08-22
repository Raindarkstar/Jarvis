@echo off
setlocal

set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python 3.12 or newer was not found. Run install.ps1 first.
        exit /b 1
    )
    set "PYTHON=python"
)

"%PYTHON%" -m jarvis_cli desktop %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
