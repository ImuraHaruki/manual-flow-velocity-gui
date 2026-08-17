@echo off
cd /d "%~dp0"
set "APP_PYTHONW=%USERPROFILE%\anaconda3\envs\flow-velocity-gui\pythonw.exe"
if not exist "%APP_PYTHONW%" (
    echo The conda environment was not found.
    echo Expected: %APP_PYTHONW%
    echo.
    echo Create it from environment.yml and try again.
    pause
    exit /b 1
)
start "" "%APP_PYTHONW%" "%~dp0app.py"
exit /b 0
