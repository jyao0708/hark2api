FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HARK2API_HOME=/app \
    HARK2API_HOST=0.0.0.0 \
    HARK2API_PORT=18611

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY .env.example README.md LICENSE ./
COPY docs/ ./docs/
RUN mkdir -p /app/data/media

VOLUME ["/app/data"]
EXPOSE 18611
CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "/app/src", "--host", "0.0.0.0", "--port", "18611"]
