@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Yakusuru
rem ---------------------------------------------------------------------------
rem  Yakusuru - Windows launcher
rem  First run: finds Python, builds a private environment (one time) and opens
rem  the app. Later runs start the app directly.   Options: --reset  --setup
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "BOOT=%ROOT%\app\bootstrap.py"
set "PY="

rem Detect the real CPU architecture (also correct inside x64 emulation on ARM PCs)
set "HWARCH=x64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "HWARCH=arm64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "HWARCH=arm64"
if "%HWARCH%"=="arm64" (
    rem Windows on ARM: native arm64 Python, max 3.13 (GUI wheels); engines use whisper.cpp
    set "VERSIONS=3.13 3.12 3.11 3.10" & set "PYTAG=-arm64" & set "WANT=ARM64" & set "WINGET_ID=Python.Python.3.13"
) else (
    set "VERSIONS=3.14 3.13 3.12 3.11 3.10" & set "PYTAG=-64" & set "WANT=AMD64" & set "WINGET_ID=Python.Python.3.14"
)
echo   Machine: Windows %HWARCH%

call :findpy

if not defined PY (
    echo.
    echo   Yakusuru needs a native %HWARCH% Python 3.10 or newer ^(see READ ME FIRST^).
    echo.
    where winget >nul 2>&1
    if not errorlevel 1 (
        set /p "ANS=  Install !WINGET_ID! now with winget? [Y/n] "
        if /i "!ANS!"=="" set "ANS=Y"
        if /i "!ANS!"=="Y" (
            winget install -e --id !WINGET_ID! --architecture !HWARCH! --scope user --accept-package-agreements --accept-source-agreements
            echo.
            echo   Looking for the new Python...
            rem This window still has the PATH from before the install: reload it, then search again
            call :refreshpath
            call :findpy
            if defined PY echo   Found it. Continuing with the setup.
        )
    )
)

if not defined PY (
    echo   Please install a %HWARCH% Python from https://www.python.org/downloads/windows/
    echo   ^(tick "Add python.exe to PATH" in the installer^), then run this again.
    start "" "https://www.python.org/downloads/windows/"
    pause
    exit /b 1
)

%PY% "%BOOT%" %*
if errorlevel 1 (
    echo.
    echo   Something went wrong - see the messages above.
    echo   Tip: run "Yakusuru.bat --reset" to rebuild the environment.
    pause
    exit /b 1
)
endlocal
exit /b 0

rem ---------------------------------------------------------------------------
:findpy
rem 1) Python launcher (py.exe): native-architecture builds only, newest supported first
for %%V in (%VERSIONS%) do if not defined PY (
    py -%%V%PYTAG% -c "import platform,sys; sys.exit(0 if platform.machine().upper()=='%WANT%' else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=py -%%V%PYTAG%"
)
rem 2) python on PATH (skips the Microsoft Store placeholder, which fails this test)
if not defined PY (
    python -c "import platform,sys; v=sys.version_info[:2]; sys.exit(0 if (3,10)<=v<=((3,13) if '%HWARCH%'=='arm64' else (3,14)) and platform.machine().upper()=='%WANT%' else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
rem 3) the standard install folders (per user and all users), in case PATH wasn't updated
for %%V in (%VERSIONS%) do if not defined PY (
    set "VN=%%V"
    set "VN=!VN:.=!"
    for %%D in ("%LOCALAPPDATA%\Programs\Python\Python!VN!" "%LOCALAPPDATA%\Programs\Python\Python!VN!-arm64" "%ProgramFiles%\Python!VN!" "%ProgramFiles%\Python!VN!-arm64") do if not defined PY (
        if exist "%%~D\python.exe" (
            "%%~D\python.exe" -c "import platform,sys; sys.exit(0 if platform.machine().upper()=='%WANT%' else 1)" >nul 2>&1
            if not errorlevel 1 set "PY="%%~D\python.exe""
        )
    )
)
exit /b 0

:refreshpath
set "NEWPATH="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','User')+';'+[Environment]::GetEnvironmentVariable('Path','Machine')"`) do set "NEWPATH=%%P"
set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%SystemRoot%;!NEWPATH!;%PATH%"
exit /b 0
