#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# EasyProxy Full - Termux One-Shot Setup (No WARP)
# ============================================================
# Usage: Open Termux, then run:
#   curl -fsSL --retry 3 https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh | bash
#
# Or copy this file and run:
#   chmod +x termux_setup.sh && ./termux_setup.sh
#
# After setup, start with:
#   easyproxy
# ============================================================

set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[INFO]${NC} $1"; }

trap 'err "Setup failed at line $LINENO: $BASH_COMMAND"' ERR

DISTRO_NAME="ubuntu"
DISTRO_IMAGE="ubuntu"
EP_DIR="/root/EasyProxy"
EP_REPO="https://github.com/realbestia1/EasyProxy.git"

echo ""
echo -e "${BLUE}==========================================${NC}"
echo -e "${BLUE}  EasyProxy Full - Termux Setup          ${NC}"
echo -e "${BLUE}  No WARP | proot-distro Ubuntu          ${NC}"
echo -e "${BLUE}==========================================${NC}"
echo ""

info "Phase 1/5: Installing Termux packages..."
termux-setup-storage 2>/dev/null || true
pkg update -y
pkg install -y proot-distro git curl pulseaudio wget screen
log "Termux packages installed."

info "Phase 2/5: Setting up Ubuntu environment..."
PROOT_DISTRO_VERSION="$(proot-distro --version 2>/dev/null || true)"
if [[ "$PROOT_DISTRO_VERSION" =~ ([0-9]+)(\.|$) ]] && (( BASH_REMATCH[1] >= 5 )); then
    DISTRO_IMAGE="ubuntu:24.04"
fi

if proot-distro login "$DISTRO_NAME" -- true >/dev/null 2>&1; then
    warn "Ubuntu is already installed, continuing..."
else
    info "Installing $DISTRO_IMAGE..."
    proot-distro install "$DISTRO_IMAGE"
    proot-distro login "$DISTRO_NAME" -- true
    log "Ubuntu installed."
fi

info "Phase 3/5: Configuring Ubuntu and installing EasyProxy..."
proot-distro login "$DISTRO_NAME" -- bash -s <<'UBUNTU_SETUP'
    set -Eeuo pipefail
    trap 'echo "[FATAL] Ubuntu setup failed at line $LINENO: $BASH_COMMAND" >&2' ERR
    export DEBIAN_FRONTEND=noninteractive

    echo "[INFO] Inside Ubuntu: Checking disk space..."
    df -h /

    echo "[INFO] Inside Ubuntu: Switching to a more reliable mirror..."
    sed -i "s|archive.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true
    sed -i "s|security.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true

    echo "[INFO] Inside Ubuntu: Adding non-snap Chromium PPA..."
    apt-get update -y
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:xtradeb/apps

    echo "[INFO] Inside Ubuntu: Updating packages..."
    apt-get update -y

    ASOUND_PACKAGE="libasound2"
    if apt-cache show libasound2t64 >/dev/null 2>&1; then
        ASOUND_PACKAGE="libasound2t64"
    fi

    echo "[INFO] Inside Ubuntu: Installing Python, browser and runtime packages..."
    apt-get install -y --fix-missing \
        python3 python3-venv python-is-python3 python3-pip git curl wget \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 "$ASOUND_PACKAGE" libpango-1.0-0 libcairo2 \
        libatspi2.0-0 fonts-liberation ca-certificates chromium chromium-driver procps \
        libxshmfence1 libglu1-mesa libx11-xcb1 libxcb-dri3-0 libxss1 libxtst6 libxslt1.1

    command -v chromium >/dev/null 2>&1
    command -v chromedriver >/dev/null 2>&1

    EP_DIR="/root/EasyProxy"
    EP_REPO="https://github.com/realbestia1/EasyProxy.git"

    if [ -d "$EP_DIR/.git" ]; then
        echo "[WARN] EasyProxy already exists, pulling latest..."
        git -C "$EP_DIR" pull --ff-only
    elif [ -e "$EP_DIR" ]; then
        echo "[FATAL] $EP_DIR exists but is not a Git checkout." >&2
        exit 1
    else
        echo "[INFO] Cloning EasyProxy..."
        git clone "$EP_REPO" "$EP_DIR"
    fi

    VENV_DIR="$EP_DIR/.venv"
    echo "[INFO] Creating the EasyProxy Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    VENV_PYTHON="$VENV_DIR/bin/python"

    echo "[INFO] Upgrading pip in the virtual environment..."
    "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel

    echo "[INFO] Installing EasyProxy requirements..."
    cd "$EP_DIR"
    "$VENV_PYTHON" -m pip install --no-cache-dir -r requirements.txt

    echo "[INFO] Playwright will use system Chromium (/usr/bin/chromium)..."
    # No need to install playwright chromium, saves ~500MB

    echo "[INFO] Setting up FlareSolverr..."
    if [ ! -d "$EP_DIR/flaresolverr/.git" ]; then
        if [ -e "$EP_DIR/flaresolverr" ]; then
            echo "[FATAL] $EP_DIR/flaresolverr exists but is not a Git checkout." >&2
            exit 1
        fi
        git clone https://github.com/FlareSolverr/FlareSolverr.git "$EP_DIR/flaresolverr"
    fi
    cd "$EP_DIR/flaresolverr"
    git checkout -- src/utils.py 2>/dev/null || true
    sed -i "s|options.add_argument('--no-sandbox')|options.add_argument('--no-sandbox'); options.add_argument('--disable-dev-shm-usage'); options.add_argument('--disable-gpu'); options.add_argument('--headless=new')|" src/utils.py
    sed -i "s|^\([[:space:]]*\)start_xvfb_display()|\1pass|g" src/utils.py 2>/dev/null || true
    sed -i "s|driver_executable_path=driver_exe_path|driver_executable_path=\"/usr/bin/chromedriver\"|" src/utils.py 2>/dev/null || true
    "$VENV_PYTHON" -m pip install --no-cache-dir -r requirements.txt

    echo "[INFO] Installing critical dependencies..."
    "$VENV_PYTHON" -m pip install --no-cache-dir uvicorn prometheus-client certifi

    if [ ! -f "$EP_DIR/.env" ]; then
        {
            echo "PORT=7860"
            echo "ENABLE_WARP=false"
        } > "$EP_DIR/.env"
    fi
UBUNTU_SETUP
log "Ubuntu environment and EasyProxy installation complete."

info "Phase 4/5: Creating the Ubuntu launcher..."
proot-distro login "$DISTRO_NAME" -- bash -c '
    set -Eeuo pipefail
    target=/root/easyproxy_start.sh
    temporary="${target}.tmp.$$"
    trap '\''rm -f "$temporary"'\'' EXIT
    cat > "$temporary"
    chmod 0755 "$temporary"
    mv -f "$temporary" "$target"
    trap - EXIT
' <<'LAUNCHER_EOF'
#!/bin/bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
LOG_DIR="/root/.easyproxy"
LOG_FILE="$LOG_DIR/easyproxy.log"
PID_FILE="$LOG_DIR/easyproxy.pid"
EP_DIR="/root/EasyProxy"
VENV_PYTHON="$EP_DIR/.venv/bin/python"
APP_PID=""

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

cleanup() {
    status=$?
    trap - EXIT INT TERM HUP
    if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
        kill "$APP_PID" 2>/dev/null || true
        wait "$APP_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo ""
echo "=================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] EasyProxy bootstrap"
echo "=================================================="

if [ -f "/usr/bin/chromium" ]; then
    export CHROME_BIN="/usr/bin/chromium"
elif [ -f "/usr/bin/chromium-browser" ]; then
    export CHROME_BIN="/usr/bin/chromium-browser"
fi

export CHROME_EXE_PATH="${CHROME_BIN:-}"
export CHROME_DRIVER_PATH="/usr/bin/chromedriver"
export FLARESOLVERR_URL=http://localhost:8191
export ENABLE_WARP="${ENABLE_WARP:-false}"

if [ ! -d "$EP_DIR" ]; then
    echo "[FATAL] $EP_DIR not found inside Ubuntu."
    exit 1
fi
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[FATAL] Python virtual environment not found at $EP_DIR/.venv."
    exit 1
fi

cd "$EP_DIR"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

PORT=${PORT:-7860}
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "[FATAL] Invalid PORT value: $PORT"
    exit 1
fi

if [ -r "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE")"
    if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[FATAL] EasyProxy is already running with PID $OLD_PID."
        exit 1
    fi
    rm -f "$PID_FILE"
fi

echo ""
echo "EasyProxy Full - Termux Edition"
echo "Port: $PORT | Mode: Headless"
echo "Python: $("$VENV_PYTHON" --version 2>/dev/null || echo missing)"
echo "Pip: $("$VENV_PYTHON" -m pip --version 2>/dev/null || echo missing)"
echo "Chromium: ${CHROME_BIN:-missing}"
echo "Chromedriver: $(command -v chromedriver 2>/dev/null || echo missing)"
echo ""

echo "FlareSolverr starts on-demand via Python code"

echo "Starting EasyProxy on port $PORT..."
"$VENV_PYTHON" app.py &
APP_PID=$!
echo "$APP_PID" > "$PID_FILE"
wait "$APP_PID"
LAUNCHER_EOF
log "Ubuntu launcher created through proot-distro login."

info "Phase 5/5: Creating Termux launcher scripts..."
mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/easyproxy" << 'CMD_EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
LOG_DIR="$HOME/.easyproxy"
TERMUX_LOG="$LOG_DIR/screen.log"

mkdir -p "$LOG_DIR"

LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || true)"
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP="$(ifconfig wlan0 2>/dev/null | awk '/inet / {print $2; exit}' || true)"
fi
LOCAL_IP="${LOCAL_IP:-localhost}"

if screen -list | grep -q "[.]easyproxy[[:space:]]"; then
    echo "EasyProxy is already running."
    echo "   Logs: easyproxy-logs"
    echo "   Stop: easyproxy-stop"
    exit 0
fi

echo "Starting EasyProxy Full in background (Screen)..."
echo "   Access (Local):   http://localhost:7860"
echo "   Access (Network): http://${LOCAL_IP}:7860"
echo "   To view logs:     easyproxy-logs"
echo "   To stop:          easyproxy-stop"
echo ""

screen -L -Logfile "$TERMUX_LOG" -dmS easyproxy \
    bash -lc 'exec proot-distro login ubuntu -- /root/easyproxy_start.sh'
sleep 3

if ! screen -list | grep -q "[.]easyproxy[[:space:]]"; then
    echo "EasyProxy exited during startup."
    echo "Last Termux/screen log lines:"
    tail -n 80 "$TERMUX_LOG" 2>/dev/null || true
    echo ""
    echo "Last Ubuntu bootstrap log lines:"
    proot-distro login ubuntu -- bash -lc "tail -n 80 /root/.easyproxy/easyproxy.log 2>/dev/null || echo No_Ubuntu_log_found_yet" 2>/dev/null || true
    exit 1
fi

echo "EasyProxy started."
CMD_EOF
chmod +x "$PREFIX/bin/easyproxy"

cat > "$PREFIX/bin/easyproxy-update" << 'UPD_EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
echo "Running full EasyProxy system update..."
easyproxy-stop 2>/dev/null || true
curl -fsSL --retry 3 --connect-timeout 20 \
    "https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh?$(date +%s)" | bash
echo "EasyProxy system updated successfully!"
easyproxy
UPD_EOF
chmod +x "$PREFIX/bin/easyproxy-update"

cat > "$PREFIX/bin/easyproxy-stop" << 'STOP_EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -u
echo "Stopping EasyProxy and all solvers..."
STOP_STATUS=0
if ! proot-distro login ubuntu -- bash -s <<'GUEST_STOP'; then
PID_FILE="/root/.easyproxy/easyproxy.pid"

if [ -r "$PID_FILE" ]; then
    APP_PID="$(cat "$PID_FILE")"
    if [[ "$APP_PID" =~ ^[0-9]+$ ]] && kill -0 "$APP_PID" 2>/dev/null; then
        kill "$APP_PID" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$APP_PID" 2>/dev/null || break
            sleep 0.25
        done
        kill -KILL "$APP_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# Compatibility cleanup for processes started by older EasyProxy launchers.
pkill -TERM -f 'python3.*(app|flaresolverr|easyproxy_start)' 2>/dev/null || true
pkill -TERM -f 'node.*flaresolverr' 2>/dev/null || true
pkill -TERM -f gunicorn 2>/dev/null || true
pkill -TERM Xvfb 2>/dev/null || true
GUEST_STOP
    echo "Warning: could not stop guest processes through proot-distro login." >&2
    STOP_STATUS=1
fi

screen -X -S easyproxy quit 2>/dev/null || true
for _ in {1..10}; do
    screen -list | grep -q "[.]easyproxy[[:space:]]" || break
    sleep 0.2
done

if screen -list | grep -q "[.]easyproxy[[:space:]]"; then
    echo "Failed to stop the EasyProxy screen session." >&2
    exit 1
fi

if [ "$STOP_STATUS" -ne 0 ]; then
    exit "$STOP_STATUS"
fi
echo "Stopped."
STOP_EOF
chmod +x "$PREFIX/bin/easyproxy-stop"

cat > "$PREFIX/bin/easyproxy-logs" << 'LOGS_EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -u
LOG_DIR="$HOME/.easyproxy"
echo "Opening logs... (Press Ctrl+A then D to exit logs without stopping)"
if screen -list | grep -q "[.]easyproxy[[:space:]]"; then
    screen -r easyproxy
    exit 0
fi

echo "No active screen session found. Showing saved logs instead."
echo ""
echo "--- Termux / screen log ---"
tail -n 80 "$LOG_DIR/screen.log" 2>/dev/null || echo "No Termux log found."
echo ""
echo "--- Ubuntu bootstrap log ---"
proot-distro login ubuntu -- bash -lc 'tail -n 120 /root/.easyproxy/easyproxy.log 2>/dev/null || echo "No Ubuntu log found."' 2>/dev/null || true
LOGS_EOF
chmod +x "$PREFIX/bin/easyproxy-logs"

log "Launcher scripts created."

echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  EasyProxy Full - Setup Complete!       ${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo -e "  ${BLUE}Start:${NC}   easyproxy"
echo -e "  ${BLUE}Update:${NC}  easyproxy-update"
echo -e "  ${BLUE}Stop:${NC}    easyproxy-stop"
echo -e "  ${BLUE}Logs:${NC}    easyproxy-logs"
echo -e "  ${BLUE}Config:${NC}  Edit inside proot:"
echo -e "           proot-distro login ubuntu"
echo -e "           nano /root/EasyProxy/.env"
echo ""
echo -e "  ${YELLOW}Access:${NC}  http://localhost:7860"
echo -e "  ${YELLOW}Note:${NC}   First start may take ~30s (Chromium init)"
echo ""
