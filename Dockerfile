FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One worker only: the collector runs in-process, and extra workers would
# each start their own, writing duplicate samples.
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8080", "app:app"]
