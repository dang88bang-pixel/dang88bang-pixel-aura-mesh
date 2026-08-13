# AURA 6.0 edge agent
FROM python:3.11-slim AS base

# Build tools are needed only for wheels that lack an aarch64 binary; they are
# removed in the same layer so they never reach the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc g++ && apt-get autoremove -y

COPY aura/ ./aura/
COPY agent.py .

# Run unprivileged. Serial access is granted per-device via --device rather
# than by running the whole container as root.
RUN useradd --create-home --uid 10001 aura \
    && mkdir -p /data && chown -R aura:aura /data /app
USER aura

ENV AURA_DB_PATH=/data/aura.db \
    AURA_HOST=0.0.0.0 \
    AURA_PORT=8080 \
    AURA_SIMULATE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["python", "agent.py"]
