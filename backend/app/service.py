from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
import time
from typing import Any

from .ozon_client import OzonClient, OzonAPIError
from .schemas import LookupResponse, NewPictureItem, SyncPicturesRequest, SyncPicturesResponse
from .storage import YandexStorageClient


class ProductPicturesService:
    def __init__(
        self,
        ozon: OzonClient,
        storage: YandexStorageClient,
        max_images_per_product: int = 30,
    ) -> None:
        self._ozon = ozon
        self._storage = storage
        self._max_images_per_product = max(1, int(max_images_per_product))

    def lookup_product(self, offer_id: str) -> LookupResponse:
        resolved = self._ozon.resolve_product(offer_id)
        info = self._ozon.get_product_info(product_id=resolved.product_id)
        pictures_from_info = self._extract_ordered_images_from_info(info)
        pictures_from_picture_api = self._ozon.get_product_pictures(
            product_id=resolved.product_id,
            offer_id=resolved.offer_id,
        )
        pictures = self._merge_with_fallback_urls(pictures_from_info, pictures_from_picture_api)

        product_name = self._extract_product_name(info) or resolved.name
        visibility = (
            info.get("visibility")
            if isinstance(info, dict)
            else None
        ) or resolved.visibility

        return LookupResponse(
            offer_id=resolved.offer_id,
            product_id=resolved.product_id,
            product_name=product_name,
            visibility=visibility,
            current_images=pictures,
        )

    @staticmethod
    def _extract_product_name(info: dict[str, Any] | None) -> str | None:
        if not isinstance(info, dict):
            return None
        for key in ("name", "title", "product_name"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_ordered_images_from_info(info: dict[str, Any] | None) -> list[str]:
        if not isinstance(info, dict):
            return []

        primary_image = ProductPicturesService._extract_urls_from_node(info.get("primary_image"))
        images = ProductPicturesService._extract_urls_from_node(info.get("images"))
        images360 = ProductPicturesService._extract_urls_from_node(info.get("images360"))
        color_image = ProductPicturesService._extract_urls_from_node(info.get("color_image"))

        ordered: list[str] = []
        ordered.extend(primary_image)
        ordered.extend(images)
        ordered.extend(images360)
        ordered.extend(color_image)
        if ordered:
            return ProductPicturesService._normalize_http_urls(ordered, dedupe=False)

        # Last-resort fallback: collect all URLs from info payload.
        return ProductPicturesService._extract_urls_from_node(info)

    @staticmethod
    def _extract_urls_from_node(node: Any) -> list[str]:
        urls: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith(("http://", "https://")):
                    urls.append(candidate)
                return
            if isinstance(value, dict):
                for inner in value.values():
                    walk(inner)
                return
            if isinstance(value, list):
                for inner in value:
                    walk(inner)

        walk(node)
        return ProductPicturesService._normalize_http_urls(urls, dedupe=False)

    @staticmethod
    def _normalize_http_urls(values: list[str], dedupe: bool) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized.startswith(("http://", "https://")):
                continue
            if dedupe:
                if normalized in seen:
                    continue
                seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def _merge_with_fallback_urls(primary: list[str], fallback: list[str]) -> list[str]:
        base = ProductPicturesService._normalize_http_urls(primary, dedupe=False)
        if not base:
            return ProductPicturesService._normalize_http_urls(fallback, dedupe=False)

        existing = set(base)
        for url in ProductPicturesService._normalize_http_urls(fallback, dedupe=False):
            if url in existing:
                continue
            existing.add(url)
            base.append(url)
        return base

    def sync_pictures(self, request: SyncPicturesRequest) -> SyncPicturesResponse:
        started_at = perf_counter()
        timings_ms: dict[str, int] = {}

        if not request.items:
            raise OzonAPIError("Пустой список изображений: передайте хотя бы один existing/new элемент")

        offer_id = request.offer_id
        product_id = request.product_id

        if product_id is None:
            resolved = self._ozon.resolve_product(offer_id)
            product_id = resolved.product_id
            offer_id = resolved.offer_id

        if request.unarchive_if_needed:
            unarchive_started = perf_counter()
            self._ozon.unarchive_products([product_id])
            timings_ms["unarchive"] = int((perf_counter() - unarchive_started) * 1000)

        prepared_items: list[dict[str, Any]] = []
        new_upload_tasks: list[dict[str, Any]] = []
        new_sequence = 1

        decode_started = perf_counter()
        for item_index, item in enumerate(request.items):
            if item.kind == "existing":
                if not item.url.startswith(("http://", "https://")):
                    raise OzonAPIError(f"Некорректный URL существующего изображения: {item.url}")
                prepared_items.append({"kind": "existing", "url": item.url})
                continue

            if not isinstance(item, NewPictureItem):
                raise OzonAPIError("Некорректный формат new изображения")

            try:
                content = base64.b64decode(item.image.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise OzonAPIError(
                    f"Не удалось декодировать base64 для {item.image.filename}: {exc}"
                ) from exc

            if not content:
                raise OzonAPIError(f"Пустой файл изображения: {item.image.filename}")

            prepared_items.append({"kind": "new", "item_index": item_index})
            new_upload_tasks.append(
                {
                    "item_index": item_index,
                    "sequence_number": new_sequence,
                    "filename": item.image.filename,
                    "content": content,
                }
            )
            new_sequence += 1

        timings_ms["decode_base64"] = int((perf_counter() - decode_started) * 1000)

        upload_started = perf_counter()
        uploaded_by_item_index = self._upload_new_images_parallel(
            offer_id=offer_id,
            tasks=new_upload_tasks,
        )
        timings_ms["upload_bucket"] = int((perf_counter() - upload_started) * 1000)

        uploaded_urls: list[str] = []
        final_urls: list[str] = []
        for prepared in prepared_items:
            if prepared["kind"] == "existing":
                final_urls.append(prepared["url"])
                continue

            item_index = int(prepared["item_index"])
            uploaded_url = uploaded_by_item_index.get(item_index)
            if not uploaded_url:
                raise OzonAPIError(
                    f"Не найден URL загруженного изображения для позиции {item_index}"
                )

            uploaded_urls.append(uploaded_url)
            final_urls.append(uploaded_url)

        if len(final_urls) > self._max_images_per_product:
            raise OzonAPIError(
                f"Превышен лимит изображений на товар: {len(final_urls)} > "
                f"{self._max_images_per_product}. "
                f"Измените OZON_MAX_IMAGES_PER_PRODUCT в backend/.env при необходимости."
            )

        import_started = perf_counter()
        ozon_response = self._ozon.import_pictures(
            image_urls=final_urls,
            product_id=product_id,
        )
        timings_ms["ozon_import"] = int((perf_counter() - import_started) * 1000)

        task_status = None
        if request.wait_import_status:
            task_started = perf_counter()
            task_status = self._wait_import_task_status(ozon_response)
            timings_ms["wait_import_status"] = int((perf_counter() - task_started) * 1000)

        apply_check = None
        if request.verify_apply:
            verify_started = perf_counter()
            apply_check = self._check_applied_images(
                product_id=product_id,
                expected_urls=final_urls,
                offer_id=offer_id,
            )
            timings_ms["verify_apply"] = int((perf_counter() - verify_started) * 1000)

        timings_ms["total"] = int((perf_counter() - started_at) * 1000)

        return SyncPicturesResponse(
            offer_id=offer_id,
            product_id=product_id,
            uploaded_urls=uploaded_urls,
            final_urls=final_urls,
            ozon_response=ozon_response,
            import_task_status=task_status,
            apply_check=apply_check,
            timings_ms=timings_ms,
        )

    def _upload_new_images_parallel(
        self,
        offer_id: str,
        tasks: list[dict[str, Any]],
    ) -> dict[int, str]:
        if not tasks:
            return {}

        if len(tasks) == 1:
            task = tasks[0]
            url = self._storage.upload_png(
                offer_id=offer_id,
                sequence_number=int(task["sequence_number"]),
                content=task["content"],
            )
            return {int(task["item_index"]): url}

        max_workers = min(4, len(tasks))
        results: dict[int, str] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(
                    self._storage.upload_png,
                    offer_id=offer_id,
                    sequence_number=int(task["sequence_number"]),
                    content=task["content"],
                ): task
                for task in tasks
            }

            for future in as_completed(future_to_task):
                task = future_to_task[future]
                item_index = int(task["item_index"])
                filename = str(task.get("filename", "unknown"))
                try:
                    results[item_index] = future.result()
                except Exception as exc:  # noqa: BLE001
                    raise OzonAPIError(
                        f"Ошибка загрузки в bucket для файла {filename}: {exc}"
                    ) from exc

        return results

    def _check_applied_images(
        self,
        product_id: int,
        expected_urls: list[str],
        offer_id: str,
    ) -> dict[str, Any]:
        expected_count = len(expected_urls)
        if expected_count == 0:
            return {
                "checked": True,
                "matched": True,
                "expected_count": 0,
                "actual_count": 0,
                "missing_count": 0,
                "missing_urls": [],
            }

        # Ozon applies picture updates asynchronously; keep this quick to avoid long UI waits.
        timeout_sec = 12
        poll_interval_sec = 2
        deadline = time.time() + timeout_sec

        last_count = -1
        current_images: list[str] = []
        while time.time() < deadline:
            try:
                info = self._ozon.get_product_info(product_id=product_id)
                current_images = self._extract_ordered_images_from_info(info)
                last_count = len(current_images)
                if last_count == expected_count:
                    break
            except OzonAPIError:
                # Network/API hiccups are common right after import; continue probing.
                pass
            time.sleep(poll_interval_sec)

        expected_set = set(expected_urls)
        actual_set = set(current_images)
        missing_urls = [url for url in expected_urls if url not in actual_set]

        return {
            "checked": True,
            "matched": len(missing_urls) == 0 and last_count == expected_count,
            "expected_count": expected_count,
            "actual_count": last_count if last_count >= 0 else len(current_images),
            "missing_count": len(missing_urls),
            "missing_urls": missing_urls[:10],
            "offer_id": offer_id,
            "product_id": product_id,
            "timeout_sec": timeout_sec,
            "same_set": expected_set == actual_set,
        }

    def _wait_import_task_status(self, ozon_response: dict[str, Any]) -> dict[str, Any] | None:
        task_id = self._extract_task_id(ozon_response)
        if task_id is None:
            return None

        deadline = time.time() + 15
        last_status: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                last_status = self._ozon.get_import_products_info(task_id)
            except OzonAPIError:
                time.sleep(1.5)
                continue

            state = self._extract_task_state(last_status)
            if state in {"done", "completed", "success", "finished", "failed", "error"}:
                return last_status
            time.sleep(1.5)

        return last_status

    @staticmethod
    def _extract_task_state(payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return ""

        candidates: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.lower() in {"status", "state", "task_status"} and isinstance(value, str):
                        candidates.append(value.lower())
                    elif isinstance(value, (dict, list)):
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return candidates[0] if candidates else ""

    @staticmethod
    def _extract_task_id(payload: dict[str, Any]) -> int | None:
        candidates = [
            payload.get("task_id"),
            payload.get("result", {}).get("task_id") if isinstance(payload.get("result"), dict) else None,
        ]

        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue

        return None
