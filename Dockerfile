# openvurp — runtime image.
# Starts headless: the wallet on :8420, the gateway, the inbound channels and
# the heartbeat. The terminal is still there: `docker compose exec openvurp openvurp`.
FROM python:3.12-slim

# git for self-update and several capabilities; curl/ca-certificates for HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Lean image: LLM backends, every inbound channel that is pure Python, PDF
# preview and security. Left out on purpose because they are heavy: whisper and
# playwright (`pip install -e ".[all]"` adds them). WhatsApp is not here either
# — its Baileys bridge needs Node, see channels/wa-bridge.
RUN pip install --no-cache-dir -e ".[openai,anthropic,groq,telegram,discord,slack,pdf,security]"

# Sensible container defaults (override them from compose or -e).
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
