#!/bin/sh
# Userspace WireGuard relay (wireproxy) control for EasyProxy.
#
# One script covers both uses:
#   * the Cloudflare WARP relay started by entrypoint.sh (reads WARP_CONFIG_FILE,
#     defaults /tmp/easyproxy-warp + 127.0.0.1:1080, probes the Cloudflare trace)
#   * the secondary tunnels managed by services/wg_tunnels.py (NordLynx or a
#     user-supplied profile: WG_CONFIG_FILE, WG_SOCKS_BIND, WG_TUNNEL_DIR,
#     WG_LOG_FILE, WG_PROBE_URL, WG_PING_URL, WG_TAIL_LINES, WG_WIREPROXY_BIN)
#
# Actions: start | stop | restart | probe | ping | logs | status
set -eu

CONFIG_FILE="${WG_CONFIG_FILE:-${WARP_CONFIG_FILE:-/etc/wireguard/wg0.conf}}"
TUNNEL_DIR="${WG_TUNNEL_DIR:-/tmp/easyproxy-warp}"
SOCKS_BIND="${WG_SOCKS_BIND:-127.0.0.1:1080}"
PROBE_URL="${WG_PROBE_URL:-https://www.cloudflare.com/cdn-cgi/trace}"
PING_URL="${WG_PING_URL:-https://api.ipify.org}"
LOG_FILE="${WG_LOG_FILE:-/var/log/wireproxy.log}"
WIREPROXY_BIN="${WG_WIREPROXY_BIN:-/usr/local/bin/wireproxy}"
TAIL_LINES="${WG_TAIL_LINES:-40}"
PID_FILE="${TUNNEL_DIR}/wireproxy.pid"
WIREPROXY_CONFIG="${TUNNEL_DIR}/wireproxy.conf"

pid_is_wireproxy() {
    pid="$1"
    [ -r "/proc/${pid}/comm" ] || return 1
    [ "$(tr -d '\n' < "/proc/${pid}/comm" 2>/dev/null)" = "wireproxy" ] || return 1
    # A dead child can linger as a zombie (its comm still matches): treat it as
    # stopped, otherwise proot/Termux waits the full timeout before giving up.
    [ "$(awk '{print $3}' "/proc/${pid}/stat" 2>/dev/null)" != "Z" ]
}

read_pid() {
    [ -s "$PID_FILE" ] || return 1
    pid=$(tr -dc '0-9' < "$PID_FILE")
    [ -n "$pid" ] || return 1
    printf '%s\n' "$pid"
}

write_wireproxy_config() {
    # Keep the tunnel IPv4-only. Remove IPv6 fields even when a manually
    # supplied/generated profile contains them.
    sed -E '/^(Address|AllowedIPs|DNS) = / {
        s/, *[^, ]*:[^, ]*//g
    }' "$CONFIG_FILE" > "$WIREPROXY_CONFIG"

    # Resolve the WireGuard endpoint to an IPv4 address as well: this keeps the
    # control-plane handshake from selecting an IPv6 endpoint implicitly.
    endpoint=$(sed -n 's/^Endpoint = //p' "$WIREPROXY_CONFIG" | head -n 1)
    endpoint_host=${endpoint%:*}
    endpoint_port=${endpoint##*:}
    endpoint_ipv4=$(getent ahostsv4 "$endpoint_host" 2>/dev/null | awk 'NR == 1 { print $1 }')
    if [ -n "$endpoint_ipv4" ] && [ -n "$endpoint_port" ]; then
        sed -i "s/^Endpoint = .*/Endpoint = ${endpoint_ipv4}:${endpoint_port}/" "$WIREPROXY_CONFIG"
    fi

    printf '\n[Socks5]\nBindAddress = %s\n' "$SOCKS_BIND" >> "$WIREPROXY_CONFIG"
    chmod 600 "$WIREPROXY_CONFIG"
}

start_wireproxy() {
    if pid=$(read_pid) && pid_is_wireproxy "$pid"; then
        echo "wireproxy already running (pid ${pid})."
        return 0
    fi

    rm -f "$PID_FILE"
    [ -x "$WIREPROXY_BIN" ] || { echo "wireproxy binary not found." >&2; return 1; }
    [ -f "$CONFIG_FILE" ] || { echo "WireGuard config not found: ${CONFIG_FILE}" >&2; return 1; }
    mkdir -p "$TUNNEL_DIR"
    mkdir -p "$(dirname "$LOG_FILE")"
    write_wireproxy_config

    if ! "$WIREPROXY_BIN" -n -c "$WIREPROXY_CONFIG" >/dev/null 2>&1; then
        echo "wireproxy config validation failed." >&2
        "$WIREPROXY_BIN" -n -c "$WIREPROXY_CONFIG" 2>&1 | tail -n 5 >&2 || true
        rm -f "$WIREPROXY_CONFIG"
        return 1
    fi

    : > "$LOG_FILE" 2>/dev/null || true
    "$WIREPROXY_BIN" -c "$WIREPROXY_CONFIG" >>"$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "Started wireproxy (pid ${pid}) on ${SOCKS_BIND}."
}

stop_wireproxy() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        rm -f "$PID_FILE"
        rm -f "$WIREPROXY_CONFIG"
        echo "wireproxy not running."
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && pid_is_wireproxy "$pid"; do
        sleep 1
        i=$((i + 1))
    done

    if pid_is_wireproxy "$pid"; then
        echo "wireproxy did not stop within 10 seconds." >&2
        return 1
    fi
    rm -f "$PID_FILE"
    rm -f "$WIREPROXY_CONFIG"
    echo "Stopped wireproxy (pid ${pid})."
}

probe_relay() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        echo "Tunnel probe: wireproxy process is down." >&2
        return 1
    fi

    # WARP (Cloudflare trace) needs local DNS resolution; the secondary tunnels
    # (probe URL supplied by wg_tunnels.py) resolve at the SOCKS endpoint.
    if [ -n "${WG_PROBE_URL:-}" ]; then
        dns_flag="--socks5-hostname"
    else
        dns_flag="--socks5"
    fi

    out=$(curl "$dns_flag" "$SOCKS_BIND" -fsS \
        --connect-timeout 3 --max-time 8 "$PROBE_URL" 2>&1) || {
        echo "Tunnel probe: SOCKS traffic failed: $out" >&2
        tail -n 3 "$LOG_FILE" 2>/dev/null >&2 || true
        return 1
    }

    printf '%s\n' "$out"
    if printf '%s\n' "$out" | grep -Eq '^warp='; then
        # Cloudflare trace: the relay must report WARP and an IPv4 egress.
        printf '%s\n' "$out" | grep -Eq '^warp=(on|plus)$' || {
            echo "WARP probe: Cloudflare did not report warp=on/plus." >&2
            return 1
        }
        printf '%s\n' "$out" | grep -Eq '^ip=[0-9.]+$' || {
            echo "WARP probe: egress is not IPv4-only." >&2
            return 1
        }
    else
        printf '%s\n' "$out" | grep -Eq '^[0-9a-fA-F:.]+$' || {
            echo "Tunnel probe: unexpected egress payload." >&2
            return 1
        }
    fi
}

# Latency check through the tunnel. time_connect would only measure the local
# SOCKS socket, so use the TLS handshake (it needs round trips through the VPN)
# plus time_total for the whole request. Last line: "<tls> <total> <code>".
ping_relay() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        echo "VPN check: wireproxy process is down." >&2
        return 1
    fi

    out=$(curl --socks5-hostname "$SOCKS_BIND" -fsS -o - \
        -w '\n%{time_appconnect} %{time_total} %{http_code}' \
        --connect-timeout 5 --max-time 12 "$PING_URL" 2>&1) || {
        echo "VPN check failed: $out" >&2
        tail -n 3 "$LOG_FILE" 2>/dev/null >&2 || true
        return 1
    }

    printf '%s\n' "$out"
}

show_logs() {
    if [ -s "$LOG_FILE" ]; then
        tail -n "$TAIL_LINES" "$LOG_FILE"
    else
        echo "No wireproxy log yet."
    fi
}

case "${1:-status}" in
    start)
        start_wireproxy
        ;;
    stop)
        stop_wireproxy
        ;;
    restart)
        stop_wireproxy
        start_wireproxy
        ;;
    probe)
        probe_relay
        ;;
    ping)
        ping_relay
        ;;
    logs)
        show_logs
        ;;
    status)
        pid=$(read_pid 2>/dev/null || true)
        if [ -n "$pid" ] && pid_is_wireproxy "$pid"; then
            echo "running (pid ${pid})"
            exit 0
        fi
        echo "stopped"
        exit 1
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|probe|ping|logs}" >&2
        exit 2
        ;;
esac
