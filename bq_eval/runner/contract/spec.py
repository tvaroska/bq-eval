"""Contracts for offline BigQuery-to-SQLite execution and fixture verification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RunnerConfig(BaseModel):
    """Runtime settings for an in-memory offline SQL runner session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_date_utc: str = "2026-11-25"
    current_timestamp_utc: str | None = None
    session_user: str = "runner@example.invalid"
    default_dataset: str | None = None


class TableFunctionSpec(BaseModel):
    """Metadata describing a registered BigQuery table-valued function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str | None
    name: str
    params: tuple[str, ...]
    canonical_key: str


class FixtureTableLoadResult(BaseModel):
    """Summary of one fixture table loaded into the offline runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_table: str
    sqlite_table: str
    row_count: int = Field(ge=0)
    columns: tuple[str, ...]


class RunnerQueryResult(BaseModel):
    """Materialized result of running a BigQuery query on the offline SQLite runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: str
    sqlite_sql: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]
    elapsed_ms: float = Field(ge=0.0)
