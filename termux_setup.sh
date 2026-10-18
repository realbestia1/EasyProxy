#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# EasyProxy Full - Termux One-Shot Setup
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
echo -e "${BLUE}  WARP + WireGuard tunnels | proot-distro Ubuntu${NC}"
echo -e "${BLUE}==========================================${NC}"
echo ""

info "Phase 1/6: Installing Termux packages..."
termux-setup-storage 2>/dev/null || true
# Termux is rolling-release and does not support partial upgrades.  Keep the
# complete native environment aligned before installing individual packages;
# otherwise curl and its OpenSSL/ngtcp2 libraries can end up ABI-incompatible.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y
pkg install -y proot-distro git curl wget screen
log "Termux packages installed."

info "Phase 2/6: Setting up Ubuntu environment..."
PROOT_DISTRO_VERSION="$(proot-distro --version 2>/dev/null || true)"
if [[ "$PROOT_DISTRO_VERSION" =~ ([0-9]+)(\.|$) ]] && (( BASH_REMATCH[1] >= 5 )); then
    # proot-distro v5 installs OCI images instead of the legacy distribution
    # aliases. Pin Ubuntu 24.04 instead of implicitly pulling "latest".
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

info "Phase 3/6: Configuring Ubuntu and installing EasyProxy..."
proot-distro login "$DISTRO_NAME" -- bash -s <<'UBUNTU_SETUP'
    set -Eeuo pipefail
    trap 'echo "[FATAL] Ubuntu setup failed at line $LINENO: $BASH_COMMAND" >&2' ERR
    export DEBIAN_FRONTEND=noninteractive

    echo "[INFO] Inside Ubuntu: Checking disk space..."
    df -h /

    echo "[INFO] Inside Ubuntu: Refreshing apt metadata..."
    apt-get update -y

    ASOUND_PACKAGE="libasound2"
    if apt-cache show libasound2t64 >/dev/null 2>&1; then
        ASOUND_PACKAGE="libasound2t64"
    fi

    echo "[INFO] Inside Ubuntu: Installing Python, Node.js and runtime packages..."
    apt-get install -y --fix-missing \
        python3 python3-venv python-is-python3 python3-pip git curl wget \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 "$ASOUND_PACKAGE" libpango-1.0-0 libcairo2 \
        libatspi2.0-0 fonts-liberation ca-certificates nodejs npm procps jq wireguard-tools \
        libxshmfence1 libglu1-mesa libx11-xcb1 libxcb-dri3-0 libxss1 libxtst6 libxslt1.1

    if ! command -v node >/dev/null 2>&1; then
        if command -v nodejs >/dev/null 2>&1; then
            NODEJS_BIN="$(command -v nodejs)"
            NODE_LINK="/usr/local/bin/node"
            if [ -e "$NODE_LINK" ] || [ -L "$NODE_LINK" ]; then
                echo "[FATAL] $NODE_LINK exists but does not provide a working node command; refusing to overwrite it." >&2
                exit 1
            fi
            echo "[INFO] Inside Ubuntu: Creating $NODE_LINK -> $NODEJS_BIN..."
            install -d -m 0755 "$(dirname "$NODE_LINK")"
            ln -s "$NODEJS_BIN" "$NODE_LINK"
        else
            echo "[FATAL] The nodejs package was installed, but neither node nor nodejs is available." >&2
            exit 1
        fi
    fi

    echo "[INFO] Inside Ubuntu: Verifying the Node.js toolchain..."
    command -v node
    node --version
    npm --version

    echo "[INFO] Inside Ubuntu: Installing wireproxy (userspace WireGuard SOCKS5 relay)..."
    WIREPROXY_VERSION="1.1.3"
    case "$(dpkg --print-architecture)" in
        amd64) WIREPROXY_ASSET="amd64" ;;
        arm64) WIREPROXY_ASSET="arm64" ;;
        armhf) WIREPROXY_ASSET="arm" ;;
        *) WIREPROXY_ASSET="" ;;
    esac
    if [ -z "$WIREPROXY_ASSET" ]; then
        echo "[WARN] Unsupported architecture for wireproxy: NordVPN and custom WireGuard tunnels stay unavailable."
    elif command -v wireproxy >/dev/null 2>&1; then
        echo "[INFO] wireproxy already installed: $(wireproxy -v)"
    else
        WIREPROXY_TARBALL="wireproxy_linux_${WIREPROXY_ASSET}.tar.gz"
        WIREPROXY_URL="https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}"
        if curl -fL --retry 3 --connect-timeout 20 -o "/tmp/${WIREPROXY_TARBALL}" "${WIREPROXY_URL}/${WIREPROXY_TARBALL}" \
            && curl -fL --retry 3 --connect-timeout 20 -o /tmp/wireproxy.checksums "${WIREPROXY_URL}/checksums.txt" \
            && (cd /tmp && grep " ${WIREPROXY_TARBALL}\$" wireproxy.checksums | sha256sum -c -) \
            && tar -xzf "/tmp/${WIREPROXY_TARBALL}" -C /usr/local/bin wireproxy \
            && chmod +x /usr/local/bin/wireproxy; then
            echo "[INFO] wireproxy installed: $(wireproxy -v)"
        else
            echo "[WARN] wireproxy install failed: NordVPN and custom WireGuard tunnels stay unavailable."
        fi
        rm -f "/tmp/${WIREPROXY_TARBALL}" /tmp/wireproxy.checksums
    fi

    echo "[INFO] Inside Ubuntu: Installing the Cloudflare WARP config generator..."
    WARP_GENERATOR_COMMIT="d4616f154d654d5c159c193432159240c96614bb"
    if command -v warp-register >/dev/null 2>&1; then
        echo "[INFO] warp-register already installed."
    elif curl -fL --retry 3 --connect-timeout 20 \
        "https://raw.githubusercontent.com/lanrat/wireguard-warp-generator/${WARP_GENERATOR_COMMIT}/scripts/warp-register.sh" \
        -o /usr/local/bin/warp-register; then
        chmod 700 /usr/local/bin/warp-register
        echo "[INFO] warp-register installed."
    else
        rm -f /usr/local/bin/warp-register
        echo "[WARN] warp-register install failed: WARP stays unavailable."
    fi

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

    if [ ! -f "$EP_DIR/.env" ]; then
        {
            echo "PORT=7860"
            echo "ENABLE_WARP=false"
        } > "$EP_DIR/.env"
    fi
UBUNTU_SETUP
log "Ubuntu environment and EasyProxy installation complete."

info "Phase 4/6: Creating the Ubuntu launcher..."
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
echo ""

echo "Starting EasyProxy on port $PORT..."

# Cloudflare WARP: register once, then expose the userspace SOCKS5 relay on
# 127.0.0.1:1080, mirroring what entrypoint.sh does in the Docker image.
WARP_CONFIG_FILE="${WARP_CONFIG_FILE:-/data/warp.conf}"
export WARP_CONFIG_FILE
if command -v warp-register >/dev/null 2>&1 && command -v wireproxy >/dev/null 2>&1; then
    if [ ! -s "$WARP_CONFIG_FILE" ]; then
        echo "Registering Cloudflare WARP (one time)..."
        mkdir -p "$(dirname "$WARP_CONFIG_FILE")"
        if WARP_DNS="1.1.1.1, 1.0.0.1" WARP_MTU="1280" WARP_ALLOWED_IPS="0.0.0.0/0" \
            WARP_PERSISTENT_KEEPALIVE="25" WARP_DEVICE_TYPE="Linux" WARP_LOCALE="en_US" \
            warp-register > "${WARP_CONFIG_FILE}.tmp"; then
            chmod 600 "${WARP_CONFIG_FILE}.tmp"
            mv -f "${WARP_CONFIG_FILE}.tmp" "$WARP_CONFIG_FILE"
            echo "WARP profile saved in ${WARP_CONFIG_FILE}."
        else
            rm -f "${WARP_CONFIG_FILE}.tmp"
            echo "WARP registration failed; continuing without WARP."
        fi
    fi
    if [ -s "$WARP_CONFIG_FILE" ] && bash "$EP_DIR/scripts/wireproxy_ctl.sh" start; then
        # Wait for the SOCKS5 listener like entrypoint.sh does in Docker.
        for _ in {1..20}; do
            if (exec 3<>/dev/tcp/127.0.0.1/1080) 2>/dev/null; then
                echo "WARP userspace SOCKS5 relay ready on 127.0.0.1:1080."
                break
            fi
            sleep 1
        done
    else
        echo "WARP tunnel unavailable; continuing without it."
    fi
else
    echo "wireproxy or warp-register missing; WARP stays disabled."
fi

"$VENV_PYTHON" app.py &
APP_PID=$!
echo "$APP_PID" > "$PID_FILE"
wait "$APP_PID"
LAUNCHER_EOF
log "Ubuntu launcher created through proot-distro login."

info "Phase 5/6: Creating Termux launcher scripts..."
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

echo "Updating Termux packages..."
# Use apt-get directly: the pkg wrapper depends on curl and cannot repair a
# partially upgraded Termux installation when curl itself no longer starts.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y

SETUP_URL="https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh?$(date +%s)"
SETUP_SCRIPT="$(mktemp)"
cleanup() {
    rm -f -- "$SETUP_SCRIPT"
}
trap cleanup EXIT

if command -v curl >/dev/null 2>&1 && \
        curl -fsSL --retry 3 --connect-timeout 20 -o "$SETUP_SCRIPT" "$SETUP_URL"; then
    :
elif command -v wget >/dev/null 2>&1 && \
        wget -q --tries=3 --timeout=20 -O "$SETUP_SCRIPT" "$SETUP_URL"; then
    :
else
    echo "Unable to download the EasyProxy setup script with curl or wget." >&2
    exit 1
fi

bash "$SETUP_SCRIPT"
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
pkill -TERM -f 'python3.*(app|easyproxy_start)' 2>/dev/null || true
pkill -TERM -x wireproxy 2>/dev/null || true
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
set -Eeuo pipefail

LOG_DIR="$HOME/.easyproxy"
TERMUX_LOG="$LOG_DIR/screen.log"

show_help() {
    cat <<'HELP'
Usage:
  easyproxy-logs             Follow the EasyProxy application log
  easyproxy-logs --termux    Follow the Termux/screen log
  easyproxy-logs --attach    Attach to the running screen session
  easyproxy-logs --help      Show this help

Press Ctrl+C to stop following a log. EasyProxy will keep running.
When using --attach, detach with Ctrl+A, then D.
HELP
}

case "${1:-}" in
    ""|--app)
        echo "Following EasyProxy application log. Press Ctrl+C to exit."
        exec proot-distro login ubuntu -- bash -lc '
            mkdir -p /root/.easyproxy
            touch /root/.easyproxy/easyproxy.log
            exec tail -n 150 -F /root/.easyproxy/easyproxy.log
        '
        ;;

    --termux)
        mkdir -p "$LOG_DIR"
        touch "$TERMUX_LOG"
        echo "Following Termux/screen log. Press Ctrl+C to exit."
        exec tail -n 100 -F "$TERMUX_LOG"
        ;;

    --attach|-a)
        if screen -list | grep -q "[.]easyproxy[[:space:]]"; then
            echo "Attaching to EasyProxy screen."
            echo "Detach with Ctrl+A, then D."
            exec screen -r easyproxy
        fi

        echo "No active EasyProxy screen session found." >&2
        exit 1
        ;;

    --help|-h)
        show_help
        ;;

    *)
        echo "Unknown option: $1" >&2
        show_help >&2
        exit 2
        ;;
esac
LOGS_EOF

chmod +x "$PREFIX/bin/easyproxy-logs"

log "Launcher scripts created."

info "Phase 6/6: Verifying the completed Ubuntu environment..."
proot-distro login "$DISTRO_NAME" -- bash -s <<'FINAL_VERIFY'
    set -uo pipefail

    failures=0

    verify_component() {
        label="$1"
        binary="$2"

        if path="$(command -v "$binary" 2>/dev/null)" && \
                version="$("$binary" --version 2>&1)" && [ -n "$version" ]; then
            version="${version%%$'\n'*}"
            printf '  %-10s %-7s %-28s %s\n' "$label" "OK" "$path" "$version"
        else
            printf '  %-10s %-7s %s\n' "$label" "MISSING" "command: $binary"
            failures=$((failures + 1))
        fi
    }

    echo ""
    echo "EasyProxy component verification:"
    printf '  %-10s %-7s %-28s %s\n' "Component" "Status" "Command" "Version"
    verify_component "Python" "python3"
    verify_component "Node" "node"
    verify_component "npm" "npm"

    if [ "$failures" -ne 0 ]; then
        echo "[FATAL] Final verification failed: $failures required component(s) are unavailable." >&2
        exit 1
    fi

    echo "[OK] All required Ubuntu components are available."
FINAL_VERIFY
log "Final component verification passed."

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
echo ""
