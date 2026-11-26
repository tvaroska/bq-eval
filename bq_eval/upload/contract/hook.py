"""Pydantic contracts for the Shape-3 asynchronous reference upload hook (`specs/shapes.md`)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PayloadProfile = Literal["ephemeral_sample", "regulated_worm"]
UploadFormat = Literal["parquet", "jsonl"]


class ReferenceUploadConfig(BaseModel):
    """Configuration for the bounded asynchronous Shape-3 upload hook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_path: str = Field(min_length=1)
    upload_format: UploadFormat = "jsonl"
    max_queue_size: int = Field(default=1024, ge=1, le=1_000_000)
    payload_profile: PayloadProfile = "ephemeral_sample"

    @field_validator("base_path")
    @classmethod
    def _validate_base_path(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned or ".." in cleaned:
            raise ValueError("base_path must be non-empty and must not contain '..'")
        return cleaned


class UploadRecord(BaseModel):
    """One Hive-partitioned BigLake Shape-3 payload record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    hr: str = Field(pattern=r"^\d{2}$")
    ref_uri: str = Field(min_length=1)
    payload: object


class UploadMetrics(BaseModel):
    """Explicit queue, delivery, and drop counters (never silent drops)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enqueued: int = Field(default=0, ge=0)
    written: int = Field(default=0, ge=0)
    dropped_queue_full: int = Field(default=0, ge=0)
    write_errors: int = Field(default=0, ge=0)
    fail_closed_rejections: int = Field(default=0, ge=0)
