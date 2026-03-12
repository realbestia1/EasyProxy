# Usa un'immagine Python con base Debian per FFmpeg completo
FROM python:3.11-slim-bookworm

# Imposta la directory di lavoro all'interno del container.
WORKDIR /app

# Installa FFmpeg con supporto DASH/CENC (versione completa)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Copia il file delle dipendenze.
# Farlo prima del resto del codice sfrutta la cache di Docker se le dipendenze non cambiano.
COPY requirements.txt .

# Installa le dipendenze Python.
RUN pip install --no-cache-dir -r requirements.txt

# Crea utente non-root per sicurezza
RUN useradd -m -u 1000 appuser

# Copia il resto del codice dell'applicazione nella directory di lavoro.
COPY . .

# Crea le directory necessarie con permessi corretti
RUN mkdir -p temp_hls recordings static && \
    chown -R appuser:appuser /app

# Metadata dell'immagine OCI (Open Container Initiative) corretti.
LABEL org.opencontainers.image.title="HLS Proxy Server"
LABEL org.opencontainers.image.description="Server proxy universale per stream HLS con supporto Vavoo, DLHD e playlist builder"
LABEL org.opencontainers.image.version="2.5.0"
LABEL org.opencontainers.image.source="https://github.com/nzo66/EasyProxy"

# Esponi la porta su cui l'applicazione è in ascolto.
EXPOSE 7860

# Health check per Render e orchestratori
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-7860}/health || exit 1

# Usa utente non-root
USER appuser

# Comando per avviare l'app in produzione con Gunicorn
CMD sh -c "gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 2 --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app"
