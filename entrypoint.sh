#!/bin/bash
export PYTHONPATH=/app

# --- Cloudflare WARP Setup ---
if [ "$ENABLE_WARP" = "true" ]; then
    echo "🌐 Starting Cloudflare WARP..."
    # Ensure /dev/net/tun exists
    if [ ! -c /dev/net/tun ]; then
        echo "⚠️ /dev/net/tun not found. WARP might not work. Ensure --cap-add=NET_ADMIN and --device /dev/net/tun are used."
    fi

    # Start warp-svc and suppress noisy hardware/dbus warnings
    warp-svc --accept-tos > /var/log/warp-svc.log 2>&1 &
    
    # Wait for warp-svc to be ready
    MAX_RETRIES=15
    COUNT=0
    while ! warp-cli --accept-tos status > /dev/null 2>&1; do
        echo "⏳ Waiting for warp-svc... ($COUNT/$MAX_RETRIES)"
        sleep 1
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then
            echo "❌ Failed to start warp-svc"
            break
        fi
    done

    if [ $COUNT -lt $MAX_RETRIES ]; then
        # Register if needed
        if ! warp-cli --accept-tos status | grep -q "Registration Name"; then
             echo "📝 Registering WARP..."
             # Delete old registration if it exists to avoid "Old registration is still around" error
             warp-cli --accept-tos registration delete > /dev/null 2>&1 || true
             warp-cli --accept-tos registration new
        fi
        
        # Set license key if provided
        if [ -n "$WARP_LICENSE_KEY" ]; then
            echo "🔑 Setting WARP license key..."
            warp-cli --accept-tos registration license "$WARP_LICENSE_KEY"
        fi
        
        # Connect
        echo "🔗 Connecting to WARP..."
        
        # Add exclusions for domains that block WARP or need real IP
        # We try both new (v2024+) and old warp-cli commands for compatibility
        for domain in cinemacity.cc cccdn.net vavoo.to vavoo.tv lokke.app mediahubmx.cc; do
            (warp-cli --accept-tos tunnel host add $domain > /dev/null 2>&1 || \
             warp-cli --accept-tos add-excluded-domain $domain > /dev/null 2>&1) || true
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
        if nc -z 127.0.0.1 1080; then
            echo "✅ WARP SOCKS5 proxy is listening on port 1080."
        else
            echo "⚠️ WARP SOCKS5 proxy not detected yet, but proceeding..."
        fi
        
        warp-cli --accept-tos status
    fi
fi

# Configure Proxy variables for sub-processes if WARP is enabled
PROXY_VARS=""
if [ "$ENABLE_WARP" = "true" ]; then
    # We use socks5h:// to ensure remote DNS resolution for sub-processes
    # We add NO_PROXY to ensure local communication (e.g. FlareSolverr -> chromedriver) doesn't use the proxy
    PROXY_VARS="HTTP_PROXY=socks5h://127.0.0.1:1080 HTTPS_PROXY=socks5h://127.0.0.1:1080 ALL_PROXY=socks5h://127.0.0.1:1080 NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1"
    echo "🌐 FlareSolverr/Byparr will use WARP proxy (excluding local traffic)..."
fi

# Start FlareSolverr in the background
echo "🚀 Starting FlareSolverr (v3 Python)..."
cd /app/flaresolverr && eval $PROXY_VARS PORT=8191 python3 src/flaresolverr.py &

# Start Byparr in the background
echo "🛡️ Starting Byparr..."
cd /app/byparr_src && eval $PROXY_VARS PORT=8192 python3 main.py &

# Start EasyProxy (Gunicorn)
echo "🎬 Starting EasyProxy..."
cd /app
WORKERS_COUNT=${WORKERS:-$(nproc 2>/dev/null || echo 1)}
gunicorn --bind 0.0.0.0:${PORT:-7860} --workers $WORKERS_COUNT --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app
