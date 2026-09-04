FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
COPY web_api/ web_api/

# Railway injects $PORT at runtime - must bind 0.0.0.0, not 127.0.0.1/localhost.
CMD ["sh", "-c", "uvicorn web_api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
