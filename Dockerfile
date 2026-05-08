# Monolithic Dockerfile for EasyProxy
# Optimized: Uses FlareSolverr v3 (Python)
# Compatible with AMD64 and ARM64 (Oracle VPS)

FROM python:3.12-slim-bookworm

# 1. Environment Settings
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV FLARESOLVERR_URL=http://localhost:8191

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    gnupg \
    gpg \
    tar \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    cloudflare-warp \
    netcat-openbsd \
    ffmpeg \
    chromium \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libxshmfence1 \
    libglu1-mesa \
    ca-certificates \
    fonts-liberation \
    chromium-driver \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Optional userspace WARP tools. They allow WARP as a local SOCKS5 proxy
# without NET_ADMIN or /dev/net/tun when WARP_MODE=wireproxy is selected.
ARG WGCF_VERSION=2.2.29
ARG WIREPROXY_VERSION=1.0.9
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) wgcf_arch="amd64"; wireproxy_arch="amd64" ;; \
        arm64) wgcf_arch="arm64"; wireproxy_arch="arm64" ;; \
        armhf) wgcf_arch="armv7"; wireproxy_arch="arm" ;; \
        *) echo "Unsupported architecture for wgcf/wireproxy: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fL "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${wgcf_arch}" -o /usr/local/bin/wgcf; \
    chmod +x /usr/local/bin/wgcf; \
    curl -fL "https://github.com/pufferffish/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${wireproxy_arch}.tar.gz" -o /tmp/wireproxy.tar.gz; \
    tar -xzf /tmp/wireproxy.tar.gz -C /tmp; \
    find /tmp -type f -name wireproxy -exec mv {} /usr/local/bin/wireproxy \; ; \
    chmod +x /usr/local/bin/wireproxy; \
    rm -f /tmp/wireproxy.tar.gz

# 3. Environment Settings
ENV PYTHONPATH=/app
ENV CHROME_EXE_PATH=/usr/bin/chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROME_DRIVER_PATH=/usr/bin/chromedriver

# 3. FlareSolverr v3 (Python)
RUN git clone https://github.com/FlareSolverr/FlareSolverr.git /app/flaresolverr \
    && cd /app/flaresolverr \
    && sed -i 's/driver_executable_path=driver_exe_path/driver_executable_path="\/usr\/bin\/chromedriver"/' src/utils.py \
    && sed -i "s|options.add_argument('--no-sandbox')|options.add_argument('--no-sandbox'); options.add_argument('--disable-dev-shm-usage'); options.add_argument('--disable-gpu'); options.add_argument('--headless=new')|" src/utils.py \
    && sed -i "s|^\([[:space:]]*\)start_xvfb_display()|\1pass|g" src/utils.py \
    && pip install --no-cache-dir -r requirements.txt

# 4. EasyProxy Dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia esplicita
COPY . .

# 5. Playwright is configured to use system Chromium (CHROME_BIN)
# No need to run 'playwright install chromium' which saves ~500MB

# Force LF line endings on shell scripts. A Windows clone via Git can leave
# CRLF in entrypoint.sh which makes bash fail at startup with
# `$'\r': command not found` and `syntax error near unexpected token $'do\r''`.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# 7. Metadata & Ports
LABEL org.opencontainers.image.title="EasyProxy Monolith"
LABEL org.opencontainers.image.description="All-in-one HLS Proxy with integrated FlareSolverr v3"
EXPOSE 7860 8191

# 8. Execution
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
