FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY backend/app /app/app
COPY backend/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; port=os.getenv('SERVER_PORT') or os.getenv('PORT') or '8000'; urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)"

CMD ["/app/entrypoint.sh"]
