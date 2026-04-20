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
python -m pip install pycryptodome --quiet
python -m playwright install chromium

:: --- 3. FlareSolverr Setup ---
echo Checking FlareSolverr...
IF NOT EXIST "flaresolverr\" (
    echo Downloading FlareSolverr...
    git clone https://github.com/FlareSolverr/FlareSolverr.git flaresolverr
    echo Installing FlareSolverr dependencies...
    pushd flaresolverr
    python -m pip install -r requirements.txt --quiet
    popd
)

:: --- 4. Byparr Setup + Patch ---
echo Checking Byparr...
IF NOT EXIST "byparr\" (
    echo Downloading Byparr...
    git clone https://github.com/ThePhaseless/Byparr.git byparr
    echo Applying Python 3.14 bypass patch to Byparr...
    pushd byparr
    :: Patch pyproject.toml safely using a Python one-liner
    python -c "import sys; p='pyproject.toml'; c=open(p, 'r', encoding='utf-8').read().replace('==3.14.*', '>=3.11'); open(p, 'w', encoding='utf-8', newline='\n').write(c)"
    echo Installing Byparr...
    python -m pip install . --quiet
    popd
)

:: --- 5. Start Solvers ---
echo Starting Solvers in background...

IF EXIST "flaresolverr\src\flaresolverr.py" (
    echo [OK] Starting FlareSolverr on port 8191...
    set PORT=8191
    start "FlareSolverr" /MIN python "flaresolverr\src\flaresolverr.py"
)

IF EXIST "byparr\main.py" (
    echo [OK] Starting Byparr on port 8192...
    set PORT=8192
    set BYPARR_PORT=8192
    start "Byparr" /MIN python "byparr\main.py"
)

:: --- 6. Start EasyProxy ---
echo.
echo Starting EasyProxy Main App...
echo -------------------------------------
:: Reset PORT for main app
set PORT=7860
set FLARESOLVERR_URL=http://localhost:8191
set BYPARR_URL=http://localhost:8192

python app.py
pause
