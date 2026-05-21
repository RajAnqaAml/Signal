@echo off
REM Wrapper script invoked by Windows Task Scheduler.
REM Runs the recorder with 10-min cadence, exits cleanly at 15:35 IST.
REM Output is appended to logs\recorder.log

setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist "logs" mkdir logs

echo. >> "logs\recorder.log"
echo === Recorder started %date% %time% === >> "logs\recorder.log"

python recorder.py --interval 600 --stop-after 15:35 >> "logs\recorder.log" 2>&1

echo === Recorder finished %date% %time% (exit %ERRORLEVEL%) === >> "logs\recorder.log"
