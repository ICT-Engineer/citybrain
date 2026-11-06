FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src:/app SIMULATOR=true PORT=8300 BRAIN_DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY src ./src
COPY mocks ./mocks
COPY models ./models
RUN useradd --create-home app && mkdir -p /app/data && chown -R app:app /app
USER app
EXPOSE 8300
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 CMD curl -fsS http://localhost:8300/health || exit 1
CMD ["sh", "-c", "uvicorn city_brain.main:app --host 0.0.0.0 --port ${PORT}"]
