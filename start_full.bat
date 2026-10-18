@echo off
TITLE EasyProxy Full Mode - Auto Setup
SETLOCAL EnableDelayedExpansion

echo Starting EasyProxy FULL Auto-Setup...
echo =====================================

:: --- 1. Set Environment ---
:: Clean __pycache__ folders to prevent import issues
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

:: Force PYTHONPATH to current directory
set PYTHONPATH=%CD%
set PYTHONUNBUFFERED=1

:: --- 2. EasyProxy Main Dependencies ---
echo Checking EasyProxy dependencies...
python -m pip install -r requirements.txt --quiet

:: --- 3. Start EasyProxy ---
echo.
echo Starting EasyProxy Main App...
echo -------------------------------------
:: Reset PORT for main app
set PORT=7860

python app.py
pause
