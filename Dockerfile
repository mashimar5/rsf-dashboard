# --- build the React frontend -------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # emits ../static/app, i.e. /static/app here

# --- serve it from Flask -------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /static/app ./static/app

# One worker only: the collector runs in-process, and extra workers would
# each start their own, writing duplicate samples.
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8080", "app:app"]
