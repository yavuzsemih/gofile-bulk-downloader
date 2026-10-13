@echo off
setlocal
cd /d "%~dp0"
title Gofile Downloader

where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python 3.10 or newer is required.
        echo Install Python from https://www.python.org/downloads/
        echo Then double-click this launcher again.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

%PYTHON% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Required package "requests" is not installed.
    choice /M "Install requests now"
    if errorlevel 2 exit /b 1
    %PYTHON% -m pip install requests
    if errorlevel 1 (
        echo Installation failed. Check your Python / pip installation.
        pause
        exit /b 1
    )
)

%PYTHON% "%~dp0gofile_bulk.py"
if errorlevel 1 (
    echo.
    echo The downloader exited with an error.
    pause
)
