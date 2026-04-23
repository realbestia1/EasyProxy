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

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[✅]${NC} $1"; }
warn() { echo -e "${YELLOW}[⚠️]${NC} $1"; }
err()  { echo -e "${RED}[❌]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[ℹ️]${NC} $1"; }

DISTRO_NAME="ubuntu"
DISTRO_ALIAS="easyproxy-env"
EP_DIR="/root/EasyProxy"
EP_REPO="https://github.com/realbestia1/EasyProxy.git"

# ============================================================
# PHASE 1: Termux packages
# ============================================================
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   EasyProxy Full - Termux Setup          ║${NC}"
echo -e "${BLUE}║   No WARP | proot-distro Ubuntu          ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

info "Phase 1/5: Installing Termux packages..."

# Allow storage access (needed for some operations)
termux-setup-storage 2>/dev/null || true

# Update and install base packages
pkg update -y
pkg install -y proot-distro git pulseaudio wget screen

log "Termux packages installed."

# ============================================================
# PHASE 2: Install Ubuntu via proot-distro
# ============================================================
info "Phase 2/5: Setting up Ubuntu environment..."

# Install Ubuntu if not already installed (ignore error if exists)
proot-distro install $DISTRO_NAME 2>/dev/null && log "Ubuntu installed." || warn "Ubuntu already installed, continuing..."

# ============================================================
# PHASE 3 & 4: Configure Ubuntu & Install EasyProxy (Merged)
# ============================================================
info "Phase 3/4: Configuring Ubuntu and Installing EasyProxy..."

proot-distro login $DISTRO_NAME -- bash -c '
    export DEBIAN_FRONTEND=noninteractive
    export PIP_BREAK_SYSTEM_PACKAGES=1
    
    echo "[ℹ️] Inside Ubuntu: Checking disk space..."
    df -h /
    
    echo "[ℹ️] Inside Ubuntu: Switching to a more reliable mirror..."
    sed -i "s|archive.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true
    sed -i "s|security.ubuntu.com|mirrors.kernel.org|g" /etc/apt/sources.list || true
    
    echo "[ℹ️] Inside Ubuntu: Adding non-snap Chromium PPA..."
    apt-get install -y software-properties-common || true
    add-apt-repository -y ppa:xtradeb/apps || true
    
    echo "[ℹ️] Inside Ubuntu: Updating packages..."
    apt-get update -y
    
    echo "[ℹ️] Inside Ubuntu: Installing bare minimum Python & stable Browser..."
    apt-get install -y --fix-missing \
        python3 python3-venv python3.13-venv python-is-python3 git curl wget ffmpeg \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libcairo2 \
        libatspi2.0-0 fonts-liberation ca-certificates chromium chromium-driver \
        libxshmfence1 libglu1-mesa libx11-xcb1 libxcb-dri3-0 libxss1 libxtst6 libxslt1.1 || true

    # Manual Pip fallback
    if ! command -v pip &> /dev/null && ! python3 -m pip --version &> /dev/null; then
        echo "[ℹ️] Inside Ubuntu: Apt pip failed, installing manually..."
        curl -sS https://bootstrap.pypa.io/get-pip.py | python3 - --break-system-packages || true
    fi

    EP_DIR="/root/EasyProxy"
    EP_REPO="https://github.com/realbestia1/EasyProxy.git"

    if [ -d "$EP_DIR" ]; then
        echo "[⚠️] EasyProxy already exists, pulling latest..."
        cd "$EP_DIR" && git pull || true
    else
        echo "[ℹ️] Cloning EasyProxy..."
        git clone "$EP_REPO" "$EP_DIR"
    fi

    echo "[ℹ️] Setting up python pip config..."
    mkdir -p ~/.config/pip
    echo "[global]" > ~/.config/pip/pip.conf
    echo "break-system-packages = true" >> ~/.config/pip/pip.conf

    echo "[ℹ️] Upgrading pip..."
    python3 -m pip install --upgrade pip setuptools wheel --break-system-packages || true

    echo "[ℹ️] Installing EasyProxy requirements..."
    cd "$EP_DIR"
    pip install --no-cache-dir --ignore-installed -r requirements.txt --break-system-packages || true

    echo "[ℹ️] Installing Playwright Chromium..."
    python3 -m playwright install chromium || true
    python3 -m playwright install-deps 2>/dev/null || true

    echo "[ℹ️] Setting up FlareSolverr & Byparr..."
    if [ ! -d "$EP_DIR/flaresolverr" ]; then
        git clone https://github.com/FlareSolverr/FlareSolverr.git "$EP_DIR/flaresolverr"
    fi
    cd "$EP_DIR/flaresolverr"
    # Reset utils.py to original state before patching (handles re-runs)
    git checkout -- src/utils.py 2>/dev/null || true
    sed -i "s|options.add_argument('--no-sandbox')|options.add_argument('--no-sandbox'); options.add_argument('--disable-dev-shm-usage'); options.add_argument('--disable-gpu'); options.add_argument('--headless=new')|" src/utils.py 2>/dev/null || true
    sed -i "s|^\([[:space:]]*\)start_xvfb_display()|\1pass|g" src/utils.py 2>/dev/null || true
    sed -i "s|driver_executable_path=driver_exe_path|driver_executable_path=\"/usr/bin/chromedriver\"|" src/utils.py 2>/dev/null || true
    pip install --no-cache-dir --ignore-installed -r requirements.txt --break-system-packages || true

    if [ ! -d "$EP_DIR/byparr_src" ]; then
        git clone https://github.com/ThePhaseless/Byparr.git "$EP_DIR/byparr_src"
    fi
    cd "$EP_DIR/byparr_src"
    sed -i "s/requires-python = .*/requires-python = \">= 3.11\"/" pyproject.toml 2>/dev/null || true
    pip install --no-cache-dir --ignore-installed . --break-system-packages || true

    # Explicitly install missing critical deps
    echo "[ℹ️] Installing critical dependencies..."
    pip install --no-cache-dir --ignore-installed uvicorn prometheus-client certifi --break-system-packages || true

    if [ ! -f "$EP_DIR/.env" ]; then
        echo "PORT=7860" > "$EP_DIR/.env"
        echo "ENABLE_WARP=false" >> "$EP_DIR/.env"
    fi
'

log "Ubuntu environment and EasyProxy installation complete."

# ============================================================
# PHASE 5: Create launcher scripts
# ============================================================
info "Phase 5/5: Creating launcher scripts..."

# Create start script for proot
PROOT_ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root"
cat > "$PROOT_ROOTFS/easyproxy_start.sh" << 'LAUNCHER_EOF'
#!/bin/bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export PIP_BREAK_SYSTEM_PACKAGES=1
export PORT=7860
export ENABLE_WARP=false

# Auto-detect Chromium path
if [ -f "/usr/bin/chromium" ]; then
    export CHROME_BIN="/usr/bin/chromium"
elif [ -f "/usr/bin/chromium-browser" ]; then
    export CHROME_BIN="/usr/bin/chromium-browser"
fi

export CHROME_EXE_PATH="$CHROME_BIN"
export CHROME_DRIVER_PATH="/usr/bin/chromedriver"
export FLARESOLVERR_URL=http://localhost:8191
export BYPARR_URL=http://localhost:8192

cd /root/EasyProxy

# Load .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs) 2>/dev/null || true
fi

PORT=${PORT:-7860}

# Kill any existing processes
pkill -9 python3 node 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     EasyProxy Full - Termux Edition      ║"
echo "║     Port: $PORT | Mode: Headless          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Start FlareSolverr
echo "🚀 Starting FlareSolverr (Headless)..."
cd /root/EasyProxy/flaresolverr && PORT=8191 python3 src/flaresolverr.py &
FLARE_PID=$!

# Start Byparr
echo "🛡️  Starting Byparr..."
cd /root/EasyProxy/byparr_src && PORT=8192 python3 main.py &
BYPARR_PID=$!

# Wait for solvers
sleep 4

# Start EasyProxy
echo "🎬 Starting EasyProxy on port $PORT..."
cd /root/EasyProxy
# Use native aiohttp runner (most compatible)
python3 -c "from app import app; from aiohttp import web; web.run_app(app, host='0.0.0.0', port=$PORT)"

# Cleanup on exit
kill $FLARE_PID $BYPARR_PID 2>/dev/null || true
LAUNCHER_EOF
chmod +x "$PROOT_ROOTFS/easyproxy_start.sh"

# Create Termux shortcut command
mkdir -p "$HOME/../usr/bin"
cat > "$PREFIX/bin/easyproxy" << 'CMD_EOF'
#!/data/data/com.termux/files/usr/bin/bash
# EasyProxy launcher - runs inside proot-distro Ubuntu

# Detect local IP (Termux compatible)
LOCAL_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7}')
[ -z "$LOCAL_IP" ] && LOCAL_IP=$(ifconfig wlan0 2>/dev/null | grep "inet " | awk '{print $2}')
[ -z "$LOCAL_IP" ] && LOCAL_IP="localhost"

echo "🎬 Starting EasyProxy Full in background (Screen)..."
echo "   Access (Local):   http://localhost:7860"
echo "   Access (Network): http://${LOCAL_IP}:7860"
echo "   To view logs:     screen -r easyproxy"
echo "   To stop:          easyproxy-stop"
echo ""
screen -dmS easyproxy proot-distro login ubuntu -- bash /root/easyproxy_start.sh
CMD_EOF
chmod +x "$PREFIX/bin/easyproxy"

cat > "$PREFIX/bin/easyproxy-update" << 'UPD_EOF'
#!/data/data/com.termux/files/usr/bin/bash
echo "🔄 Running full EasyProxy system update..."
easyproxy-stop 2>/dev/null || true
curl -sL https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh | bash
echo "✅ EasyProxy system updated successfully!"
easyproxy
UPD_EOF
chmod +x "$PREFIX/bin/easyproxy-update"

# Create stop command  
cat > "$PREFIX/bin/easyproxy-stop" << 'STOP_EOF'
#!/data/data/com.termux/files/usr/bin/bash
echo "🛑 Stopping EasyProxy..."
proot-distro login ubuntu -- bash -c 'pkill -f flaresolverr; pkill -f byparr; pkill -f "aiohttp\|gunicorn\|app:app"; pkill Xvfb' 2>/dev/null
screen -X -S easyproxy quit 2>/dev/null || true
echo "✅ Stopped."
STOP_EOF
chmod +x "$PREFIX/bin/easyproxy-stop"

# Create logs command
cat > "$PREFIX/bin/easyproxy-logs" << 'LOGS_EOF'
#!/data/data/com.termux/files/usr/bin/bash
echo "📖 Opening logs... (Press Ctrl+A then D to exit logs without stopping)"
screen -r easyproxy
LOGS_EOF
chmod +x "$PREFIX/bin/easyproxy-logs"

log "Launcher scripts created."

# ============================================================
# DONE
# ============================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✅ EasyProxy Full - Setup Complete!    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
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
