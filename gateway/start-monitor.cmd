@echo off
setlocal
rem Launch Craft Gateway Monitor without a console window.
rem We resolve pythonw.exe next to the first `python` on PATH so the monitor
rem runs on the same interpreter as `python gatewayctl.py`; if there is no
rem matching pythonw.exe we fall back to pythonw.exe on PATH.
set "PYW="
for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PYW if exist "%%~dpIpythonw.exe" set "PYW=%%~dpIpythonw.exe"
)
if not defined PYW (
    for /f "delims=" %%I in ('where pythonw 2^>nul') do (
        if not defined PYW set "PYW=%%I"
    )
)
if not defined PYW (
    echo Could not find pythonw.exe on PATH. 1>&2
    exit /b 1
)
start "" "%PYW%" "%~dp0monitor.py" %*
