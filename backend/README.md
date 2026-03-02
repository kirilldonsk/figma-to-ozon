# Figma to Ozon Backend - Deployment Notes

This backend is a FastAPI service that:
- receives product image payloads;
- uploads new images to Yandex Object Storage;
- sends final image URLs to Ozon Seller API.

## 1. Required environment variables

Create `backend/.env` from `backend/.env.example` and fill:
- `OZON_CLIENT_ID`
- `OZON_API_KEY`
- `YC_ACCESS_KEY_ID`
- `YC_SECRET_ACCESS_KEY`
- `YC_BUCKET`

Optional runtime settings:
- `SERVER_HOST` (default `0.0.0.0`)
- `SERVER_PORT` (default `8000`)
- `LOG_LEVEL` (default `INFO`)
- `UVICORN_WORKERS` (default `1`)
- `UVICORN_LOG_LEVEL` (default `info`)

If your hosting provider injects `PORT`, `entrypoint.sh` maps it to `SERVER_PORT`.

## 2. Local run

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## 3. Docker run

Build image:

```bash
docker build -t figma-to-ozon-backend ./backend
```

Run container:

```bash
docker run --name figma-to-ozon-backend \
  --env-file backend/.env \
  -p 8000:8000 \
  --restart unless-stopped \
  figma-to-ozon-backend
```

The container has built-in healthcheck to `GET /health`.

## 4. Reverse proxy (optional)

If you expose the backend through Nginx/Caddy/Traefik:
- proxy to `127.0.0.1:8000`;
- keep `X-Forwarded-*` headers enabled;
- use TLS on the public endpoint.

## 5. Minimal pre-deploy checklist

1. Confirm `backend/.env` has valid Ozon + Yandex credentials.
2. Confirm bucket policy/ACL allows Ozon to fetch uploaded images.
3. Start backend and verify `GET /health` returns `{"status":"ok"}`.
4. Test one `POST /api/products/lookup` and one `POST /api/products/sync-pictures`.
