FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir . "PyMySQL[rsa]>=1.1,<2.0"

COPY frontend/dist/client ./frontend/dist/client
COPY deploy/cloud-test/cloud_app.py ./deploy/cloud-test/cloud_app.py

EXPOSE 8000
CMD ["uvicorn", "cloud_app:app", "--app-dir", "/app/deploy/cloud-test", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
