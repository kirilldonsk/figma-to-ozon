from __future__ import annotations

from functools import lru_cache
import logging
import threading
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError
import requests

from .config import Settings, get_settings
from .ozon_client import OzonAPIError, OzonClient
from .schemas import LookupRequest, LookupResponse, SyncPicturesRequest, SyncPicturesResponse
from .service import ProductPicturesService
from .storage import YandexStorageClient

app = FastAPI(
    title="Figma to Ozon backend",
    version="0.1.0",
    description="Uploads images to Yandex Object Storage and updates Ozon product pictures.",
)

logger = logging.getLogger("figma_to_ozon")
_cleanup_thread: threading.Thread | None = None
_cleanup_stop_event = threading.Event()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@lru_cache
def _load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        missing_fields = [
            str(error.get("loc", ["unknown"])[0])
            for error in exc.errors()
            if error.get("type") == "missing"
        ]
        if missing_fields:
            fields = ", ".join(sorted(set(missing_fields)))
            raise RuntimeError(
                f"Не заполнены обязательные переменные в backend/.env: {fields}"
            ) from exc
        raise RuntimeError(f"Ошибка конфигурации .env: {exc}") from exc


@lru_cache
def _storage_client() -> YandexStorageClient:
    settings = _load_settings()
    return YandexStorageClient(settings)


@lru_cache
def _service() -> ProductPicturesService:
    settings = _load_settings()
    ozon = OzonClient(settings)
    storage = _storage_client()
    return ProductPicturesService(
        ozon=ozon,
        storage=storage,
        max_images_per_product=settings.ozon_max_images_per_product,
    )


def _service_or_http() -> ProductPicturesService:
    try:
        return _service()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _cleanup_loop(stop_event: threading.Event) -> None:
    try:
        settings = _load_settings()
        storage = _storage_client()
    except RuntimeError as exc:
        logger.warning("YC cleanup scheduler stopped: %s", exc)
        return

    if not settings.yc_cleanup_enabled:
        logger.info("YC cleanup scheduler is disabled")
        return

    startup_delay = max(0, int(settings.yc_cleanup_startup_delay_sec))
    interval_sec = max(1, int(settings.yc_cleanup_interval_hours)) * 3600
    retention_hours = max(1, int(settings.yc_cleanup_retention_hours))
    batch_size = max(1, min(1000, int(settings.yc_cleanup_batch_size)))
    max_delete = max(1, int(settings.yc_cleanup_max_delete_per_run))

    logger.info(
        "YC cleanup scheduler started: bucket=%s prefix=%s interval_h=%s retention_h=%s",
        storage.bucket,
        storage.prefix or "/",
        settings.yc_cleanup_interval_hours,
        settings.yc_cleanup_retention_hours,
    )

    if stop_event.wait(startup_delay):
        return

    while not stop_event.is_set():
        started_at = time.monotonic()
        stats = storage.cleanup_old_objects(
            retention_hours=retention_hours,
            batch_size=batch_size,
            max_delete_per_run=max_delete,
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)

        logger.info(
            "YC cleanup done: scanned=%s deleted=%s errors=%s elapsed_ms=%s",
            stats.get("scanned", 0),
            stats.get("deleted", 0),
            stats.get("errors", 0),
            elapsed_ms,
        )

        if stop_event.wait(interval_sec):
            break


@app.on_event("startup")
def _startup_validate_settings() -> None:
    settings = _load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("Configuration loaded")


@app.on_event("startup")
def _startup_cleanup_scheduler() -> None:
    global _cleanup_thread

    try:
        settings = _load_settings()
    except RuntimeError as exc:
        logger.warning("YC cleanup scheduler skipped on startup: %s", exc)
        return

    if not settings.yc_cleanup_enabled:
        return

    if _cleanup_thread and _cleanup_thread.is_alive():
        return

    _cleanup_stop_event.clear()
    _cleanup_thread = threading.Thread(
        target=_cleanup_loop,
        args=(_cleanup_stop_event,),
        name="yc-cleanup-scheduler",
        daemon=True,
    )
    _cleanup_thread.start()


@app.on_event("shutdown")
def _shutdown_cleanup_scheduler() -> None:
    _cleanup_stop_event.set()
    if _cleanup_thread and _cleanup_thread.is_alive():
        _cleanup_thread.join(timeout=3)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/image-proxy")
def image_proxy(url: str) -> Response:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Разрешены только http/https URL")

    try:
        upstream = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось загрузить изображение: {exc}") from exc

    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Источник изображения вернул HTTP {upstream.status_code}",
        )

    media_type = upstream.headers.get("Content-Type", "image/jpeg")
    if not media_type.lower().startswith("image/"):
        media_type = "image/jpeg"

    return Response(
        content=upstream.content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.post("/api/products/lookup", response_model=LookupResponse)
def lookup_product(request: LookupRequest) -> LookupResponse:
    try:
        return _service_or_http().lookup_product(request.offer_id.strip())
    except OzonAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/products/sync-pictures", response_model=SyncPicturesResponse)
def sync_pictures(request: SyncPicturesRequest) -> SyncPicturesResponse:
    try:
        sanitized_request = request.model_copy(update={"offer_id": request.offer_id.strip()})
        return _service_or_http().sync_pictures(sanitized_request)
    except OzonAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/storage/cleanup")
def cleanup_storage_now() -> dict[str, int | str]:
    try:
        settings = _load_settings()
        storage = _storage_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not settings.yc_cleanup_enabled:
        raise HTTPException(status_code=400, detail="YC cleanup disabled in config")

    stats = storage.cleanup_old_objects(
        retention_hours=settings.yc_cleanup_retention_hours,
        batch_size=settings.yc_cleanup_batch_size,
        max_delete_per_run=settings.yc_cleanup_max_delete_per_run,
    )
    return {
        "status": "ok",
        "bucket": storage.bucket,
        "prefix": storage.prefix,
        "retention_hours": settings.yc_cleanup_retention_hours,
        **stats,
    }
