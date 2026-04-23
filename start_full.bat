@echo off
TITLE EasyProxy Full Mode - Auto Setup
SETLOCAL EnableDelayedExpansion

echo Starting EasyProxy FULL Auto-Setup...
echo =====================================

set "FLARESOLVERR_PORT=8191"
set "BYPARR_PORT=8192"

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
) ELSE (
    :: Ensure FlareSolverr is NOT headless on Windows to avoid blocks
    pushd flaresolverr
    python -c "import sys; p='src/utils.py'; c=open(p, 'r', encoding='utf-8').read(); n=c.replace('; options.add_argument(\'--disable-dev-shm-usage\'); options.add_argument(\'--disable-gpu\'); options.add_argument(\'--headless=new\')', '') if '--headless=new' in c else c; open(p, 'w', encoding='utf-8', newline='\n').write(n)"
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
    powershell -NoProfile -Command ^
        "$resp = $null; try { $resp = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%FLARESOLVERR_PORT%/' -TimeoutSec 3 } catch {}; " ^
        "if ($resp -and $resp.Content -match 'FlareSolverr') { exit 0 } else { exit 1 }" >nul 2>&1
    IF ERRORLEVEL 1 (
        echo [OK] Starting FlareSolverr on port %FLARESOLVERR_PORT%...
        set PORT=%FLARESOLVERR_PORT%
        start "FlareSolverr" /MIN python "flaresolverr\src\flaresolverr.py"
    ) ELSE (
        echo [OK] FlareSolverr already active on port %FLARESOLVERR_PORT%.
    )
)

IF EXIST "byparr\main.py" (
    powershell -NoProfile -Command "exit $(if (Test-NetConnection -ComputerName '127.0.0.1' -Port %BYPARR_PORT% -InformationLevel Quiet) { 0 } else { 1 })" >nul 2>&1
    IF ERRORLEVEL 1 (
        echo [OK] Starting Byparr on port %BYPARR_PORT%...
        set PORT=%BYPARR_PORT%
        start "Byparr" /MIN python "byparr\main.py"
    ) ELSE (
        echo [OK] Byparr already active on port %BYPARR_PORT%.
    )
)

:: --- 6. Start EasyProxy ---
echo.
echo Starting EasyProxy Main App...
echo -------------------------------------
:: Reset PORT for main app
set PORT=7860
set FLARESOLVERR_URL=http://localhost:%FLARESOLVERR_PORT%
set BYPARR_URL=http://localhost:%BYPARR_PORT%

python app.py
pause
