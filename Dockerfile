# Dockerfile.light - Versione leggera senza FlareSolverr/Byparr integrati
# Ideale per uso con Docker Compose o solver esterni.

FROM python:3.12-slim-bookworm

# Imposta la directory di lavoro all'interno del container.
WORKDIR /app

# Copia il file delle dipendenze per sfruttare la cache.
COPY requirements.txt .

# Runtime flags for browser-assisted extractors in containers.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
ENV PYTHONUNBUFFERED=1

# Installa FFmpeg e Chromium di sistema (importante per molti extractor).
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    && rm -rf /var/lib/apt/lists/*

# Installa le dipendenze Python.
RUN pip install --no-cache-dir -r requirements.txt

# Installa Chromium gestito da Playwright.
RUN python -m playwright install chromium

# Copia il resto del codice dell'applicazione nella directory di lavoro.
COPY . .

# Metadata dell'immagine
LABEL org.opencontainers.image.title="HLS Proxy Server (Light)"
LABEL org.opencontainers.image.description="Server proxy universale per stream HLS. Richiede FlareSolverr/Byparr esterni."

# Esponi la porta predefinita
EXPOSE 7860

# Comando per avviare l'app
CMD ["sh", "-c", "WORKERS_COUNT=${WORKERS:-$(nproc 2>/dev/null || echo 1)}; gunicorn --bind 0.0.0.0:${PORT:-7860} --workers $WORKERS_COUNT --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app"]
