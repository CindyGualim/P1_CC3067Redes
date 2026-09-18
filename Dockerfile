FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0 \
    PORT=8080 \
    PHARMACY_DB=/tmp/pharmacy.db

WORKDIR /app

COPY requirements-remote.txt ./
RUN pip install --no-cache-dir -r requirements-remote.txt \
    && useradd --create-home --uid 10001 pharmacy

COPY --chown=pharmacy:pharmacy src ./src
COPY --chown=pharmacy:pharmacy data/pharmacy_seed.json ./data/pharmacy_seed.json

USER pharmacy

CMD ["python", "-m", "servers.pharmacy.remote"]

