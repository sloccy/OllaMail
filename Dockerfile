FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 1000 -s /sbin/nologin appuser

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

USER appuser

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--call", "app.server:create_app"]
