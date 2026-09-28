"""Parquet writer with stable schemas derived from the local tables."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Boolean, Date, Integer, String, Text

from stallion_tally.database.models import (
    DATASET_TABLES,
    INTERNAL_COLUMNS,
    JsonText,
    MoneyType,
    UtcDateTime,
)
from stallion_tally.utils.hashing import canonical_json

DECIMAL_TYPE = pa.decimal128(28, 6)
_QUANT = Decimal("0.000001")
EXPORT_META_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("_exported_at", pa.timestamp("us", tz="UTC")),
    ("_batch_id", pa.string()),
    ("_run_id", pa.string()),
)


def _arrow_type(column: Any) -> pa.DataType:
    col_type = column.type
    if isinstance(col_type, MoneyType):
        return DECIMAL_TYPE
    if isinstance(col_type, UtcDateTime):
        return pa.timestamp("us", tz="UTC")
    if isinstance(col_type, JsonText):
        return pa.string()
    if isinstance(col_type, Boolean):
        return pa.bool_()
    if isinstance(col_type, Integer):
        return pa.int64()
    if isinstance(col_type, Date):
        return pa.date32()
    if isinstance(col_type, (String, Text)):
        return pa.string()
    return pa.string()


def export_columns(dataset: str, include_raw_json: bool = True) -> list[str]:
    table = DATASET_TABLES[dataset].__table__  # type: ignore[attr-defined]
    return [
        c.name
        for c in table.columns
        if c.name not in INTERNAL_COLUMNS and (include_raw_json or c.name != "raw_json")
    ]


def export_schema(dataset: str, include_raw_json: bool = True) -> pa.Schema:
    table = DATASET_TABLES[dataset].__table__  # type: ignore[attr-defined]
    fields = [
        pa.field(c.name, _arrow_type(c), nullable=True)
        for c in table.columns
        if c.name in export_columns(dataset, include_raw_json)
    ]
    fields.extend(pa.field(name, dtype, nullable=True) for name, dtype in EXPORT_META_COLUMNS)
    return pa.schema(fields)


def _convert(value: Any, arrow_type: pa.DataType) -> Any:
    if value is None:
        return None
    if pa.types.is_decimal(arrow_type):
        return Decimal(value).quantize(_QUANT, rounding=ROUND_HALF_UP)
    if pa.types.is_string(arrow_type) and not isinstance(value, str):
        return canonical_json(value)
    return value


def row_to_record(
    row: Any,
    dataset: str,
    *,
    exported_at: datetime,
    batch_id: str,
    run_id: str | None,
    include_raw_json: bool = True,
) -> dict[str, Any]:
    schema = export_schema(dataset, include_raw_json)
    record: dict[str, Any] = {}
    for field in schema:
        if field.name.startswith("_"):
            continue
        record[field.name] = _convert(getattr(row, field.name), field.type)
    record["_exported_at"] = exported_at
    record["_batch_id"] = batch_id
    record["_run_id"] = run_id
    return record


@dataclass(frozen=True)
class WrittenFile:
    path: Path
    record_count: int
    size_bytes: int
    sha256: str
    md5: bytes


def write_parquet(path: Path, records: list[dict[str, Any]], schema: pa.Schema) -> WrittenFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=schema)
    tmp = path.with_name(path.name + ".part")
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)
    sha = hashlib.sha256()
    md5 = hashlib.md5()  # noqa: S324 - Content-MD5 integrity check
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return WrittenFile(path, table.num_rows, path.stat().st_size, sha.hexdigest(), md5.digest())


def read_parquet(path: Path) -> pa.Table:
    return pq.read_table(path)
