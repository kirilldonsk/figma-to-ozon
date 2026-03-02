from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import requests

from .config import Settings


class OzonAPIError(RuntimeError):
    pass


@dataclass
class ResolvedProduct:
    offer_id: str
    product_id: int
    visibility: str | None = None
    name: str | None = None


class OzonClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ozon_base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Client-Id": settings.ozon_client_id,
                "Api-Key": settings.ozon_api_key,
                "Content-Type": "application/json",
            }
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_network_error: Exception | None = None

        for attempt in range(3):
            try:
                response = self._session.post(url, json=payload, timeout=60)
            except requests.RequestException as exc:
                last_network_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise OzonAPIError(f"Ошибка соединения с Ozon: {exc}") from exc

            try:
                data = response.json()
            except ValueError:
                snippet = response.text[:500]
                raise OzonAPIError(f"Invalid JSON from Ozon ({response.status_code}): {snippet}")

            if response.status_code >= 500 and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code >= 400:
                message = data.get("message") if isinstance(data, dict) else str(data)
                raise OzonAPIError(f"Ozon HTTP {response.status_code}: {message}")

            if isinstance(data, dict) and data.get("error"):
                raise OzonAPIError(f"Ozon error: {data['error']}")

            return data if isinstance(data, dict) else {"result": data}

        if last_network_error:
            raise OzonAPIError(f"Ошибка соединения с Ozon: {last_network_error}") from last_network_error
        raise OzonAPIError("Не удалось выполнить запрос к Ozon")

    def resolve_product(self, offer_id: str) -> ResolvedProduct:
        target_offer_id = self._normalize_offer_id(offer_id)

        info = self.get_product_info(offer_id=target_offer_id)
        if info:
            resolved = self._resolved_from_item(info, fallback_offer_id=target_offer_id)
            if resolved:
                return resolved

        payload = {
            "filter": {
                "offer_id": [target_offer_id],
                "visibility": "ALL",
            },
            "last_id": "",
            "limit": 100,
        }
        data = self._post("/v3/product/list", payload)
        items = self._extract_product_items(data)

        if not items:
            raise OzonAPIError(f"Товар с offer_id='{target_offer_id}' не найден в Ozon")

        exact_items = [item for item in items if self._item_matches_offer(item, target_offer_id)]
        exact_candidates = self._resolved_candidates_from_items(exact_items, fallback_offer_id=target_offer_id)

        if len(exact_candidates) == 1:
            return exact_candidates[0]

        if len(exact_candidates) > 1:
            ids = ", ".join(str(item.product_id) for item in exact_candidates)
            raise OzonAPIError(
                f"По offer_id='{target_offer_id}' найдено несколько product_id: {ids}. Уточните артикул."
            )

        all_candidates = self._resolved_candidates_from_items(items, fallback_offer_id=target_offer_id)
        if len(all_candidates) == 1:
            verified = self.get_product_info(product_id=all_candidates[0].product_id)
            if verified and self._item_matches_offer(verified, target_offer_id):
                verified_candidate = self._resolved_from_item(verified, fallback_offer_id=target_offer_id)
                if verified_candidate:
                    return verified_candidate

        candidate_ids = ", ".join(str(item.product_id) for item in all_candidates[:5]) or "none"
        raise OzonAPIError(
            f"Не найдено точного совпадения по offer_id='{target_offer_id}'. "
            f"Ozon вернул другие product_id: {candidate_ids}"
        )

    def get_product_info(
        self,
        product_id: int | None = None,
        offer_id: str | None = None,
    ) -> dict[str, Any]:
        if product_id is None and not offer_id:
            raise OzonAPIError("Нужно передать product_id или offer_id")

        normalized_offer_id = self._normalize_offer_id(offer_id) if offer_id else None
        last_error: OzonAPIError | None = None

        for path in ("/v2/product/info/list", "/v3/product/info/list"):
            for payload in self._id_payload_variants(product_id=product_id, offer_id=normalized_offer_id):
                try:
                    data = self._post(path, payload)
                    items = self._extract_product_items(data)
                    if not items:
                        continue

                    if normalized_offer_id:
                        for item in items:
                            if self._item_matches_offer(item, normalized_offer_id):
                                return item
                        if len(items) == 1:
                            return items[0]
                        continue

                    return items[0]
                except OzonAPIError as exc:
                    last_error = exc
                    continue

        if last_error:
            raise last_error
        return {}

    def get_product_pictures(self, product_id: int | None = None, offer_id: str | None = None) -> list[str]:
        if product_id is None and not offer_id:
            return []

        normalized_offer_id = self._normalize_offer_id(offer_id) if offer_id else None
        payload_variants = self._id_payload_variants(product_id=product_id, offer_id=normalized_offer_id)
        collected: list[str] = []

        for path in ("/v2/product/pictures/info", "/v1/product/pictures/info"):
            for payload in payload_variants:
                try:
                    data = self._post(path, payload)
                    collected.extend(self._extract_urls(data.get("result", data)))
                except OzonAPIError:
                    continue

        return self._unique_urls(collected)

    def unarchive_products(self, product_ids: list[int]) -> dict[str, Any]:
        payload = {"product_id": product_ids}
        return self._post("/v1/product/unarchive", payload)

    def import_pictures(
        self,
        image_urls: list[str],
        product_id: int | None = None,
        offer_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_urls = [url.strip() for url in image_urls if isinstance(url, str) and url.strip()]
        if not normalized_urls:
            raise OzonAPIError("Список изображений пуст")

        payload: dict[str, Any] = {
            "images": normalized_urls,
        }
        if product_id is not None:
            payload["product_id"] = product_id
        elif offer_id:
            payload["offer_id"] = self._normalize_offer_id(offer_id)
        else:
            raise OzonAPIError("Нужно передать product_id или offer_id")

        return self._post("/v1/product/pictures/import", payload)

    def get_import_products_info(self, task_id: int) -> dict[str, Any]:
        payload = {"task_id": task_id}
        return self._post("/v1/product/import/info", payload)

    @staticmethod
    def _extract_product_items(data: dict[str, Any]) -> list[dict[str, Any]]:
        result = data.get("result", data)
        candidates: list[dict[str, Any]] = []

        if isinstance(result, dict):
            for key in ("items", "products"):
                value = result.get(key)
                if isinstance(value, list):
                    candidates.extend([item for item in value if isinstance(item, dict)])
                elif isinstance(value, dict):
                    candidates.append(value)

            if OzonClient._extract_product_id(result) is not None:
                candidates.append(result)

        elif isinstance(result, list):
            candidates.extend([item for item in result if isinstance(item, dict)])

        unique: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in candidates:
            product_id = OzonClient._extract_product_id(item)
            if product_id is None:
                continue
            if product_id in seen:
                continue
            seen.add(product_id)
            unique.append(item)

        return unique

    @staticmethod
    def _resolved_from_item(item: dict[str, Any], fallback_offer_id: str) -> ResolvedProduct | None:
        product_id = OzonClient._extract_product_id(item)
        if product_id is None:
            return None

        known_offer_ids = OzonClient._collect_offer_ids(item)
        offer_id = known_offer_ids[0] if known_offer_ids else fallback_offer_id

        return ResolvedProduct(
            offer_id=offer_id,
            product_id=product_id,
            visibility=OzonClient._extract_text_field(item, ("visibility",)),
            name=OzonClient._extract_text_field(item, ("name", "title", "product_name")),
        )

    @staticmethod
    def _resolved_candidates_from_items(
        items: list[dict[str, Any]],
        fallback_offer_id: str,
    ) -> list[ResolvedProduct]:
        candidates: list[ResolvedProduct] = []
        seen: set[int] = set()

        for item in items:
            resolved = OzonClient._resolved_from_item(item, fallback_offer_id=fallback_offer_id)
            if not resolved:
                continue
            if resolved.product_id in seen:
                continue
            seen.add(resolved.product_id)
            candidates.append(resolved)

        return candidates

    @staticmethod
    def _item_matches_offer(item: dict[str, Any], target_offer_id: str) -> bool:
        normalized_target = OzonClient._normalize_offer_id(target_offer_id).casefold()
        for known_offer_id in OzonClient._collect_offer_ids(item):
            if OzonClient._normalize_offer_id(known_offer_id).casefold() == normalized_target:
                return True
        return False

    @staticmethod
    def _collect_offer_ids(node: Any) -> list[str]:
        found: list[str] = []

        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = "".join(ch for ch in key.lower() if ch.isalnum())

                if normalized_key == "offerid":
                    if isinstance(value, str) and value.strip():
                        found.append(value.strip())
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, str) and item.strip():
                                found.append(item.strip())

                if isinstance(value, (dict, list)):
                    found.extend(OzonClient._collect_offer_ids(value))

        elif isinstance(node, list):
            for item in node:
                found.extend(OzonClient._collect_offer_ids(item))

        deduped: list[str] = []
        seen: set[str] = set()
        for value in found:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)

        return deduped

    @staticmethod
    def _extract_product_id(item: dict[str, Any]) -> int | None:
        for key in ("product_id", "productId", "id"):
            if key not in item:
                continue
            try:
                return int(item[key])
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_text_field(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_urls(node: Any) -> list[str]:
        urls: list[str] = []

        def walk(current: Any) -> None:
            if isinstance(current, str):
                if current.startswith("http://") or current.startswith("https://"):
                    urls.append(current)
                return

            if isinstance(current, dict):
                for value in current.values():
                    walk(value)
                return

            if isinstance(current, list):
                for item in current:
                    walk(item)

        walk(node)
        return OzonClient._unique_urls(urls)

    @staticmethod
    def _unique_urls(values: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique

    @staticmethod
    def _id_payload_variants(
        product_id: int | None = None,
        offer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []

        if product_id is not None:
            variants.append({"product_id": [product_id]})
            variants.append({"product_id": product_id})

        if offer_id:
            normalized_offer_id = OzonClient._normalize_offer_id(offer_id)
            variants.append({"offer_id": [normalized_offer_id]})
            variants.append({"offer_id": normalized_offer_id})

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for payload in variants:
            key = tuple(sorted((k, str(v)) for k, v in payload.items()))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(payload)

        return deduped

    @staticmethod
    def _normalize_offer_id(value: str | None) -> str:
        return str(value or "").strip()
