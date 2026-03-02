from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

import boto3

from .config import Settings


class YandexStorageClient:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.yc_bucket
        self._prefix = settings.yc_prefix.strip("/")
        self._endpoint_url = settings.yc_endpoint_url.rstrip("/")
        self._public_base_url = (
            settings.yc_public_base_url.rstrip("/") if settings.yc_public_base_url else None
        )
        self._object_acl = settings.yc_object_acl

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.yc_endpoint_url,
            region_name=settings.yc_region,
            aws_access_key_id=settings.yc_access_key_id,
            aws_secret_access_key=settings.yc_secret_access_key,
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def upload_png(self, offer_id: str, sequence_number: int, content: bytes) -> str:
        normalized_offer_id = self._normalize_segment(offer_id)
        date_path = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        unique_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        filename = f"{normalized_offer_id}_{sequence_number:02d}_{unique_suffix}.png"

        key_parts = [self._prefix, date_path, normalized_offer_id, filename]
        object_key = "/".join(part for part in key_parts if part)

        put_args = {
            "Bucket": self._bucket,
            "Key": object_key,
            "Body": content,
            "ContentType": "image/png",
        }
        if self._object_acl:
            put_args["ACL"] = self._object_acl

        self._client.put_object(**put_args)

        return self._public_url(object_key)

    def cleanup_old_objects(
        self,
        retention_hours: int,
        batch_size: int = 1000,
        max_delete_per_run: int = 5000,
    ) -> dict[str, int]:
        retention_hours = max(1, int(retention_hours))
        batch_size = max(1, min(1000, int(batch_size)))
        max_delete_per_run = max(1, int(max_delete_per_run))

        prefix = f"{self._prefix}/" if self._prefix else ""
        threshold = datetime.now(timezone.utc) - timedelta(hours=retention_hours)

        scanned = 0
        deleted = 0
        errors = 0
        pending_keys: list[dict[str, str]] = []

        paginator = self._client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=self._bucket, Prefix=prefix, PaginationConfig={"PageSize": batch_size})

        for page in page_iterator:
            contents = page.get("Contents", [])
            if not contents:
                continue

            for obj in contents:
                scanned += 1
                if deleted >= max_delete_per_run:
                    break

                key = obj.get("Key")
                last_modified = obj.get("LastModified")
                if not isinstance(key, str) or not last_modified:
                    continue

                if last_modified <= threshold:
                    pending_keys.append({"Key": key})

                if len(pending_keys) >= 1000:
                    d, e = self._delete_batch(pending_keys)
                    deleted += d
                    errors += e
                    pending_keys = []

            if deleted >= max_delete_per_run:
                break

        if pending_keys and deleted < max_delete_per_run:
            allowed = max_delete_per_run - deleted
            d, e = self._delete_batch(pending_keys[:allowed])
            deleted += d
            errors += e

        return {
            "scanned": scanned,
            "deleted": deleted,
            "errors": errors,
        }

    def _delete_batch(self, objects: list[dict[str, str]]) -> tuple[int, int]:
        if not objects:
            return 0, 0

        try:
            response = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": objects, "Quiet": True},
            )
        except Exception:
            return 0, len(objects)

        deleted = len(response.get("Deleted", []) or [])
        errors = len(response.get("Errors", []) or [])
        return deleted, errors

    def _public_url(self, object_key: str) -> str:
        quoted_key = quote(object_key, safe="/")

        if self._public_base_url:
            if "{bucket}" in self._public_base_url:
                base = self._public_base_url.format(bucket=self._bucket)
            else:
                base = self._public_base_url
            return f"{base}/{quoted_key}"

        return f"{self._endpoint_url}/{self._bucket}/{quoted_key}"

    @staticmethod
    def _normalize_segment(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
        return cleaned or "offer"
