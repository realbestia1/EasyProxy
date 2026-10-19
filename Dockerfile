# Monolithic Dockerfile for EasyProxy
# Optimized EasyProxy runtime
# Compatible with AMD64 and ARM64 (Oracle VPS)

FROM python:3.12-slim-bookworm

# 1. Environment Settings
WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    jq \
    wireguard-tools \
    tor \
    tor-geoipdb \
    tar \
    nodejs \
    node-undici \
    netcat-openbsd \
    procps \
    ffmpeg \
    fonts-dejavu \
    chromium \
    chromium-common \
    chromium-driver \
    xvfb \
    xauth \
    dumb-init \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# FlareSolverr is part of this image, but EasyProxy starts it only on-demand
# when VixSrc returns a Cloudflare challenge.
ARG FLARESOLVERR_VERSION=3.5.2
RUN set -eux; \
    git clone --depth 1 --branch "v${FLARESOLVERR_VERSION}" \
        https://github.com/FlareSolverr/FlareSolverr.git /opt/flaresolverr; \
    pip install --no-cache-dir -r /opt/flaresolverr/requirements.txt; \
    rm -rf /opt/flaresolverr/.git

# WARP config generator and stable userspace SOCKS5 relay.
# The generator is pinned so image rebuilds remain reproducible. It is used
# only on first startup when /data/warp.conf does not exist.
ARG WARP_GENERATOR_COMMIT=d4616f154d654d5c159c193432159240c96614bb
ARG WIREPROXY_VERSION=1.1.3
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) wireproxy_arch="amd64" ;; \
        arm64) wireproxy_arch="arm64" ;; \
        armhf) wireproxy_arch="arm" ;; \
        *) echo "Unsupported architecture for wireproxy: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fL "https://raw.githubusercontent.com/lanrat/wireguard-warp-generator/${WARP_GENERATOR_COMMIT}/scripts/warp-register.sh" -o /usr/local/bin/warp-register; \
    chmod 700 /usr/local/bin/warp-register; \
    curl -fL "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${wireproxy_arch}.tar.gz" -o /tmp/wireproxy.tar.gz; \
    curl -fL "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/checksums.txt" -o /tmp/wireproxy.checksums; \
    checksum="$(awk -v asset="wireproxy_linux_${wireproxy_arch}.tar.gz" '$2 == asset { print $1 }' /tmp/wireproxy.checksums)"; \
    test -n "$checksum"; \
    printf '%s  /tmp/wireproxy.tar.gz\n' "$checksum" | sha256sum -c -; \
    tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy; \
    chmod +x /usr/local/bin/wireproxy; \
    rm -f /tmp/wireproxy.tar.gz /tmp/wireproxy.checksums; \
    mkdir -p /etc/wireguard

# 2. EasyProxy Dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 3. Environment Settings
ENV PYTHONPATH=/app
ENV FLARESOLVERR_DIR=/opt/flaresolverr
ENV FLARESOLVERR_LOG_LEVEL=error

# Copia esplicita
COPY . .

# Windows checkouts may carry CRLF into shell scripts; Linux must execute LF.
RUN sed -i 's/\r$//' entrypoint.sh scripts/wireproxy_ctl.sh

# Node's ESM resolver does not search Debian's global module directory for a
# bare import. Expose the apt-installed undici package from the app module
# tree so the VidFast runner can use HTTP ProxyAgent when WARP is selected.
RUN mkdir -p /app/node_modules \
    && ln -s /usr/share/nodejs/undici /app/node_modules/undici

# FlareSolverr uses this Docker marker to avoid downloading an
# undetected_chromedriver binary for the wrong CPU architecture. Debian's
# chromedriver comes from the same package set as Chromium above.
RUN ln -sf "$(command -v chromedriver)" /app/chromedriver

RUN chmod +x entrypoint.sh scripts/wireproxy_ctl.sh

# 5. Metadata & Ports
LABEL org.opencontainers.image.title="EasyProxy Monolith"
LABEL org.opencontainers.image.description="All-in-one HLS Proxy with integrated CF Turnstile Solver"
EXPOSE 7860
VOLUME ["/data"]

# 6. Execution
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
