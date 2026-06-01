FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8010

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Runtime write locations used by the backend.
RUN mkdir -p /app/data/deals /app/data/reports /app/data/pdfs /app/data/audit /app/logs

# Run as non-root user while keeping runtime folders writable.
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8010

# Runtime data/logs can be mounted to persist JSON/PDF outputs.
VOLUME ["/app/data", "/app/logs"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010", "--log-level", "info"]
