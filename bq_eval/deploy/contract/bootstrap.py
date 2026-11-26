"""Bootstrap mode and offline validation report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BootstrapMode = Literal["light", "full", "hybrid", "silver"]


class BootstrapValidationReport(BaseModel):
    """Deterministic result of `bq-eval bootstrap` offline validation and optional GCP `--dry-run`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: BootstrapMode
    project_id: str
    dataset_id: str
    connection_id: str
    payload_uri_pattern: str
    statement_count: int = Field(..., ge=0)
    checked_count: int = Field(..., ge=0)
    skipped_count: int = Field(..., ge=0)
    schema_column_count: int = Field(..., ge=0)
    unresolved_placeholders: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    live_dry_run_executed: bool = False
    live_apply_executed: bool = False

    @property
    def passed(self) -> bool:
        return not self.errors and not self.unresolved_placeholders
