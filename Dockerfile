# --- Build stage: install dependencies into a virtualenv-like prefix ---
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Runtime stage: copy only what's needed to run, not build tools ---
FROM python:3.12-slim

# Run as a non-root user — standard hardening for any container you'd
# actually deploy, not just leave running as root by default.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /install /usr/local
COPY app.py .
COPY src/ ./src/
COPY .streamlit/ ./.streamlit/

RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

ENV SMARTSPEND_DB_PATH=/app/data/smartspend.db \
    PYTHONUNBUFFERED=1

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["streamlit", "run", "app.py"]
