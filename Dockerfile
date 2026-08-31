# AIFlow — slim, non-root, healthchecked image.
FROM python:3.12-slim

# uvloop/httptools are optional speedups; keep the image lean otherwise.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# deps first so code edits don't bust the layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/

# data/ is a volume; the DB lives here and must survive container rebuilds
RUN mkdir -p /app/data && \
    adduser --disabled-password --gecos "" --uid 10001 aiflow && \
    chown -R aiflow:aiflow /app
USER aiflow

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
