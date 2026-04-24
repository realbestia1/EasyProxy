#!/bin/bash
export PYTHONPATH=/app

WARP_EXCLUDED_HOSTS="${WARP_EXCLUDED_HOSTS:-cinemacity.cc,*.cinemacity.cc,cccdn.net,*.cccdn.net,strem.fun,*.strem.fun,torrentio.strem.fun,real-debrid.com,*.real-debrid.com,realdebrid.com,*.realdebrid.com,api.real-debrid.com,premiumize.me,*.premiumize.me,www.premiumize.me,alldebrid.com,*.alldebrid.com,api.alldebrid.com,debrid-link.com,*.debrid-link.com,debridlink.com,*.debridlink.com,api.debrid-link.com,torbox.app,*.torbox.app,api.torbox.app,offcloud.com,*.offcloud.com,api.offcloud.com,put.io,*.put.io,api.put.io}"
WARP_LICENSE_KEY="${WARP_LICENSE_KEY:-}"

# --- Cloudflare WARP Setup ---
if [ "$ENABLE_WARP" = "true" ]; then
    echo "Starting Cloudflare WARP..."
    if [ ! -c /dev/net/tun ]; then
        echo "Warning: /dev/net/tun not found. Ensure --cap-add=NET_ADMIN and --device /dev/net/tun are used."
    fi

    warp-svc --accept-tos > /var/log/warp-svc.log 2>&1 &

    MAX_RETRIES=15
    COUNT=0
    while ! warp-cli --accept-tos status > /dev/null 2>&1; do
        echo "Waiting for warp-svc... ($COUNT/$MAX_RETRIES)"
        sleep 1
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then
            echo "Failed to start warp-svc"
            break
        fi
    done

    if [ $COUNT -lt $MAX_RETRIES ]; then
        if ! warp-cli --accept-tos status | grep -q "Registration Name"; then
            echo "Registering WARP..."
            warp-cli --accept-tos registration delete > /dev/null 2>&1 || true
            warp-cli --accept-tos registration new
        fi

        if [ -n "$WARP_LICENSE_KEY" ]; then
            echo "Setting WARP license key..."
            warp-cli --accept-tos registration license "$WARP_LICENSE_KEY"
        fi

        echo "Connecting to WARP..."

        IFS=',' read -ra WARP_EXCLUDED_HOSTS_LIST <<< "$WARP_EXCLUDED_HOSTS"
        for domain in "${WARP_EXCLUDED_HOSTS_LIST[@]}"; do
            domain="$(echo "$domain" | xargs)"
            [ -z "$domain" ] && continue
            (
                warp-cli --accept-tos tunnel host add "$domain" > /dev/null 2>&1 || \
                warp-cli --accept-tos add-excluded-domain "$domain" > /dev/null 2>&1
            ) || true
        done

        # Set mode to Proxy (SOCKS5 mode)
        warp-cli --accept-tos mode proxy
        # Set proxy port to 1080
        warp-cli --accept-tos proxy port 1080
        
        warp-cli --accept-tos connect
        
        # Small delay for connection to stabilize
        echo "⏳ Waiting for WARP to stabilize (10s)..."
        sleep 10
        
        # Check if SOCKS5 proxy is actually listening
        if command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 1080; then
            echo "✅ WARP SOCKS5 proxy is listening on port 1080."
        else
            echo "⚠️ WARP SOCKS5 proxy not detected on port 1080 yet, but proceeding..."
        fi
        
        warp-cli --accept-tos status

    fi
fi

PROXY_VARS=""
SOLVERS_FORCE_WARP_PROXY="${SOLVERS_FORCE_WARP_PROXY:-false}"
if [ "$ENABLE_WARP" = "true" ] && [ "$SOLVERS_FORCE_WARP_PROXY" = "true" ]; then
    PROXY_VARS="HTTP_PROXY=socks5://127.0.0.1:1080 HTTPS_PROXY=socks5://127.0.0.1:1080 NO_PROXY=localhost,127.0.0.1"
    echo "FlareSolverr/Byparr forced to use WARP SOCKS5 proxy globally: socks5://127.0.0.1:1080"
else
    echo "FlareSolverr/Byparr will use per-request routing from EasyProxy (supports real warp=off bypass)."
fi

echo "Starting FlareSolverr (v3 Python)..."
cd /app/flaresolverr && eval $PROXY_VARS PORT=8191 python3 src/flaresolverr.py &

echo "Starting Byparr..."
cd /app/byparr_src && eval $PROXY_VARS PORT=8192 python3 main.py &

echo "Starting EasyProxy..."
cd /app
WORKERS_COUNT=${WORKERS:-$(nproc 2>/dev/null || echo 1)}
gunicorn --bind 0.0.0.0:${PORT:-7860} --workers $WORKERS_COUNT --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app
