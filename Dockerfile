# openvurp — immagine runtime.
# Parte subito in modalità headless: dashboard web (con chat), gateway,
# Telegram e heartbeat. La TUI si apre con `docker compose exec openvurp openvurp`.
FROM python:3.12-slim

# git serve all'auto-update e a varie capability; curl/ca-certificates per HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Immagine snella: backend LLM + Telegram + sicurezza. Niente whisper/playwright
# (pesanti); aggiungibili con `pip install -e ".[all]"`. L'install registra anche
# i comandi `openvurp` e `openvurp`.
RUN pip install --no-cache-dir -e ".[openai,anthropic,groq,telegram,security]"

# Default sensati per il container (sovrascrivibili da compose/-e).
ENV PYTHONUNBUFFERED=1 \
    LLM_BACKEND=ollama \
    LLM_BASE_URL=http://host.docker.internal:11434 \
    DASHBOARD_ENABLED=true \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8420 \
    GATEWAY_ENABLED=true \
    GATEWAY_HOST=0.0.0.0

EXPOSE 8420 8421

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["headless"]
