#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# EasyProxy Full - Termux One-Shot Setup (No WARP)
# ============================================================
# Usage: Open Termux, then run:
#   curl -sL https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh | bash
#
# Or copy this file and run:
#   chmod +x termux_setup.sh && ./termux_setup.sh
#
# After setup, start with:
#   easyproxy
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[INFO]${NC} $1"; }

DISTRO_NAME="ubuntu"
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
pkg install -y proot-distro git pulseaudio wget screen
log "Termux packages installed."

info "Phase 2/5: Setting up Ubuntu environment..."
proot-distro install "$DISTRO_NAME" 2>/dev/null && log "Ubuntu installed." || warn "Ubuntu already installed, continuing..."

info "Phase 3/4: Configuring Ubuntu and installing EasyProxy..."
proot-distro login "$DISTRO_NAME" -- bash -c '
    export DEBIAN_FRONTEND=noninteractive
    export PIP_BREAK_SYSTEM_PACKAGES=1

    echo "[INFO] Inside Ubuntu: Checking disk space..."
    df -h /

    echo "[INFO] Inside Ubuntu: Switching to a more reliable mirror..."
    sed -i "s|archive.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true
    sed -i "s|security.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true

    echo "[INFO] Inside Ubuntu: Adding non-snap Chromium PPA..."
    apt-get install -y software-properties-common || true
    add-apt-repository -y ppa:xtradeb/apps || true

    echo "[INFO] Inside Ubuntu: Updating packages..."
    apt-get update -y

    echo "[INFO] Inside Ubuntu: Installing Python, browser and runtime packages..."
    apt-get install -y --fix-missing \
        python3 python3-venv python3.13-venv python-is-python3 python3-pip git curl wget ffmpeg \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libcairo2 \
        libatspi2.0-0 fonts-liberation ca-certificates chromium chromium-driver procps \
        libxshmfence1 libglu1-mesa libx11-xcb1 libxcb-dri3-0 libxss1 libxtst6 libxslt1.1 || true

    if ! command -v pip >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
        echo "[INFO] Inside Ubuntu: Apt pip missing, installing manually..."
        curl -sS https://bootstrap.pypa.io/get-pip.py | python3 - --break-system-packages || true
    fi

    EP_DIR="/root/EasyProxy"
    EP_REPO="https://github.com/realbestia1/EasyProxy.git"

    if [ -d "$EP_DIR/.git" ]; then
        echo "[WARN] EasyProxy already exists, pulling latest..."
        cd "$EP_DIR" && git pull || true
    elif [ -d "$EP_DIR" ]; then
        echo "[WARN] EasyProxy directory exists without .git, reusing it."
    else
        echo "[INFO] Cloning EasyProxy..."
        git clone "$EP_REPO" "$EP_DIR"
    fi

    echo "[INFO] Setting up python pip config..."
    mkdir -p ~/.config/pip
    {
        echo "[global]"
        echo "break-system-packages = true"
    } > ~/.config/pip/pip.conf

    echo "[INFO] Upgrading pip..."
    python3 -m pip install --upgrade pip setuptools wheel --break-system-packages || true

    echo "[INFO] Installing EasyProxy requirements..."
    cd "$EP_DIR"
    python3 -m pip install --no-cache-dir --ignore-installed -r requirements.txt --break-system-packages || true

    echo "[INFO] Installing Playwright Chromium..."
    python3 -m playwright install chromium || true
    python3 -m playwright install-deps 2>/dev/null || true

    echo "[INFO] Setting up FlareSolverr and Byparr..."
    if [ ! -d "$EP_DIR/flaresolverr/.git" ]; then
        rm -rf "$EP_DIR/flaresolverr" 2>/dev/null || true
        git clone https://github.com/FlareSolverr/FlareSolverr.git "$EP_DIR/flaresolverr"
    fi
    cd "$EP_DIR/flaresolverr"
    git checkout -- src/utils.py 2>/dev/null || true
    sed -i "s|options.add_argument('\''--no-sandbox'\'')|options.add_argument('\''--no-sandbox'\''); options.add_argument('\''--disable-dev-shm-usage'\''); options.add_argument('\''--disable-gpu'\''); options.add_argument('\''--headless=new'\'')|" src/utils.py 2>/dev/null || true
    sed -i "s|^\([[:space:]]*\)start_xvfb_display()|\1pass|g" src/utils.py 2>/dev/null || true
    sed -i "s|driver_executable_path=driver_exe_path|driver_executable_path=\"/usr/bin/chromedriver\"|" src/utils.py 2>/dev/null || true
    python3 -m pip install --no-cache-dir --ignore-installed -r requirements.txt --break-system-packages || true

    if [ ! -d "$EP_DIR/byparr_src/.git" ]; then
        rm -rf "$EP_DIR/byparr_src" 2>/dev/null || true
        git clone https://github.com/ThePhaseless/Byparr.git "$EP_DIR/byparr_src"
    fi
    cd "$EP_DIR/byparr_src"
    sed -i "s/requires-python = .*/requires-python = \">= 3.11\"/" pyproject.toml 2>/dev/null || true
    python3 -m pip install --no-cache-dir --ignore-installed . --break-system-packages || true
    python3 -m camoufox fetch || true

    echo "[INFO] Installing critical dependencies..."
    python3 -m pip install --no-cache-dir --ignore-installed uvicorn prometheus-client certifi --break-system-packages || true

    if [ ! -f "$EP_DIR/.env" ]; then
        {
            echo "PORT=7860"
            echo "ENABLE_WARP=false"
        } > "$EP_DIR/.env"
    fi
'
log "Ubuntu environment and EasyProxy installation complete."

info "Phase 5/5: Creating launcher scripts..."
PROOT_ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root"

cat > "$PROOT_ROOTFS/easyproxy_start.sh" << 'LAUNCHER_EOF'
#!/bin/bash
set -u
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export PIP_BREAK_SYSTEM_PACKAGES=1
export PORT=7860
export ENABLE_WARP=false
LOG_DIR="/root/.easyproxy"
LOG_FILE="$LOG_DIR/easyproxy.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

cleanup() {
    kill "${FLARE_PID:-}" "${BYPARR_PID:-}" 2>/dev/null || true
}

trap cleanup EXIT

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
export BYPARR_URL=http://localhost:8192

if [ ! -d /root/EasyProxy ]; then
    echo "[FATAL] /root/EasyProxy not found inside Ubuntu."
    exit 1
fi

cd /root/EasyProxy

if [ -f .env ]; then
    export $(grep -v "^#" .env | xargs) 2>/dev/null || true
fi

PORT=${PORT:-7860}

pkill -9 -f "python3.*(app|flaresolverr|byparr|main.py|easyproxy_start)" 2>/dev/null || true
pkill -9 -f "node.*flaresolverr" 2>/dev/null || true

echo ""
echo "EasyProxy Full - Termux Edition"
echo "Port: $PORT | Mode: Headless"
echo "Python: $(python3 --version 2>/dev/null || echo missing)"
echo "Pip: $(python3 -m pip --version 2>/dev/null || echo missing)"
echo "Chromium: ${CHROME_BIN:-missing}"
echo "Chromedriver: $(command -v chromedriver 2>/dev/null || echo missing)"
echo ""

echo "Starting FlareSolverr (Headless)..."
cd /root/EasyProxy/flaresolverr && PORT=8191 python3 src/flaresolverr.py &
FLARE_PID=$!

echo "Starting Byparr..."
cd /root/EasyProxy/byparr_src && PORT=8192 python3 main.py &
BYPARR_PID=$!

sleep 4

echo "Starting EasyProxy on port $PORT..."
cd /root/EasyProxy
python3 -c "from app import app; from aiohttp import web; web.run_app(app, host='0.0.0.0', port=$PORT)"
LAUNCHER_EOF
chmod +x "$PROOT_ROOTFS/easyproxy_start.sh"

mkdir -p "$HOME/../usr/bin"
cat > "$PREFIX/bin/easyproxy" << 'CMD_EOF'
#!/data/data/com.termux/files/usr/bin/bash
LOG_DIR="$HOME/.easyproxy"
TERMUX_LOG="$LOG_DIR/screen.log"

mkdir -p "$LOG_DIR"

LOCAL_IP=$(ip route get 1.1.1.1 2>/dev/null | awk "{print \$7}")
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(ifconfig wlan0 2>/dev/null | grep "inet " | awk "{print \$2}")
[ -z "$LOCAL_IP" ] && LOCAL_IP="localhost"

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

screen -L -Logfile "$TERMUX_LOG" -dmS easyproxy bash -lc "proot-distro login ubuntu -- bash /root/easyproxy_start.sh"
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
echo "Running full EasyProxy system update..."
easyproxy-stop 2>/dev/null || true
curl -sL "https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh?$(date +%s)" | bash
echo "EasyProxy system updated successfully!"
easyproxy
UPD_EOF
chmod +x "$PREFIX/bin/easyproxy-update"

cat > "$PREFIX/bin/easyproxy-stop" << 'STOP_EOF'
#!/data/data/com.termux/files/usr/bin/bash
echo "Stopping EasyProxy and all solvers..."
proot-distro login ubuntu -- bash -c 'pkill -9 -f "python3.*(app|flaresolverr|byparr|main.py|easyproxy_start)"; pkill -9 -f "gunicorn"; pkill -9 Xvfb' 2>/dev/null
screen -X -S easyproxy quit 2>/dev/null || true
pkill -9 -f "proot-distro.*ubuntu" 2>/dev/null || true
echo "Stopped."
STOP_EOF
chmod +x "$PREFIX/bin/easyproxy-stop"

cat > "$PREFIX/bin/easyproxy-logs" << 'LOGS_EOF'
#!/data/data/com.termux/files/usr/bin/bash
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
