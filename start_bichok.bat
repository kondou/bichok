@echo off
REM BicHoc launcher for Windows.
REM
REM BicHoc is a single page that runs straight from the filesystem: there is no
REM server to start and nothing to install, so this only opens the page in your
REM default browser. Double-clicking contest_log_analyzer.html does the same
REM thing; this exists so there is one obvious way in on every platform.
REM
REM SkookumNet live reception is macOS-only and is not available here. The
REM SkookumNet button simply does not appear; everything else works.

cd /d "%~dp0"

if not exist "contest_log_analyzer.html" (
    echo contest_log_analyzer.html was not found.
    echo Keep this script in the same folder as contest_log_analyzer.html.
    pause >nul
    exit /b 1
)

if not exist "chart.min.js" (
    echo Warning: chart.min.js is not in this folder, so no chart will be drawn.
)

start "" "contest_log_analyzer.html"
