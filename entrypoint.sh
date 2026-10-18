#!/bin/bash
export PYTHONPATH=/app

WARP_LICENSE_KEY="${WARP_LICENSE_KEY:-}"
WARP_PROXY_HOST="127.0.0.1"
WARP_PROXY_PORT="1080"
WARP_DIR="/tmp/easyproxy-warp"
WARP_CONFIG_FILE="${WARP_CONFIG_FILE:-/data/warp.conf}"
WARP_GENERATOR="/usr/local/bin/warp-register"
WARPCTL="/app/scripts/wireproxy_ctl.sh"
export WARP_CONFIG_FILE

start_userspace_warp() {
    echo "Starting Cloudflare WARP via saved config + wireproxy userspace SOCKS5..."

    if ! command -v "$WARP_GENERATOR" >/dev/null 2>&1 || \
       ! command -v wireproxy >/dev/null 2>&1; then
        echo "WARP generator or wireproxy not found. Rebuild the image."
        return 1
    fi

    mkdir -p "$WARP_DIR"
    mkdir -p "$(dirname "$WARP_CONFIG_FILE")"

    if [ ! -s "$WARP_CONFIG_FILE" ]; then
        echo "No saved WARP config; registering once and saving to ${WARP_CONFIG_FILE}."
        temp_config="${WARP_CONFIG_FILE}.tmp.$$"
        generator_args=()
        if [ -n "$WARP_LICENSE_KEY" ]; then
            generator_args+=(--license "$WARP_LICENSE_KEY")
        fi
        if ! WARP_DNS="1.1.1.1, 1.0.0.1" \
             WARP_MTU="1280" \
             WARP_ALLOWED_IPS="0.0.0.0/0" \
             WARP_PERSISTENT_KEEPALIVE="25" \
             WARP_DEVICE_TYPE="Linux" \
             WARP_LOCALE="en_US" \
             "$WARP_GENERATOR" "${generator_args[@]}" > "$temp_config"; then
            rm -f "$temp_config"
            echo "WARP registration failed; saved config was not changed." >&2
            return 1
        fi
        chmod 600 "$temp_config"
        mv -f "$temp_config" "$WARP_CONFIG_FILE"
        echo "Saved WARP config in ${WARP_CONFIG_FILE}."
    else
        echo "Reusing saved WARP config: ${WARP_CONFIG_FILE}."
    fi

    "$WARPCTL" start || return 1

    echo "Waiting for wireproxy SOCKS5 on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}..."
    for i in $(seq 1 20); do
        if ! "$WARPCTL" status >/dev/null 2>&1; then
            echo "wireproxy exited during startup."
            return 1
        fi
        if nc -z "$WARP_PROXY_HOST" "$WARP_PROXY_PORT" && \
           "$WARPCTL" probe >/dev/null 2>&1; then
            echo "WARP userspace WireGuard + wireproxy SOCKS5 ready on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}."
            return 0
        fi
        sleep 1
    done

    echo "wireproxy SOCKS5 not detected."
    return 1
}

# EasyProxy watchdog checks wireproxy/WARP and reconnects after consecutive failures.
cleanup() {
    "$WARPCTL" stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

start_userspace_warp || echo "WARP unavailable; EasyProxy will continue without the WARP proxy."

echo "Starting EasyProxy..."
cd /app || exit 1
python app.py
