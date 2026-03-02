from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class LookupRequest(BaseModel):
    offer_id: str = Field(..., min_length=1, description="Seller offer ID (артикул)")


class LookupResponse(BaseModel):
    offer_id: str
    product_id: int
    product_name: str | None = None
    visibility: str | None = None
    current_images: list[str] = Field(default_factory=list)


class NewImagePayload(BaseModel):
    id: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    mime_type: str = "image/png"
    content_base64: str = Field(..., min_length=1)


class ExistingPictureItem(BaseModel):
    kind: Literal["existing"]
    url: str = Field(..., min_length=1)


class NewPictureItem(BaseModel):
    kind: Literal["new"]
    image: NewImagePayload


OrderItem = Annotated[ExistingPictureItem | NewPictureItem, Field(discriminator="kind")]


class SyncPicturesRequest(BaseModel):
    offer_id: str = Field(..., min_length=1)
    product_id: int | None = None
    unarchive_if_needed: bool = False
    wait_import_status: bool = False
    verify_apply: bool = False
    items: list[OrderItem] = Field(default_factory=list)


class SyncPicturesResponse(BaseModel):
    offer_id: str
    product_id: int
    uploaded_urls: list[str] = Field(default_factory=list)
    final_urls: list[str] = Field(default_factory=list)
    ozon_response: dict
    import_task_status: dict | None = None
    apply_check: dict | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
