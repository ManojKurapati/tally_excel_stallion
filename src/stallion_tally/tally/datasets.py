"""Registry of the datasets the connector understands."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from stallion_tally.config import SYNC_DATASETS

DatasetKind = Literal["company", "master", "snapshot", "transaction"]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    kind: DatasetKind
    per_company: bool
    date_ranged: bool
    description: str
    export_children: tuple[str, ...] = ()


DATASET_SPECS: dict[str, DatasetSpec] = {
    "companies": DatasetSpec(
        "companies", "company", False, False, "Companies loaded in TallyPrime"
    ),
    "groups": DatasetSpec("groups", "master", True, False, "Account groups"),
    "ledgers": DatasetSpec("ledgers", "master", True, False, "Ledgers"),
    "stock_items": DatasetSpec("stock_items", "master", True, False, "Stock items"),
    "bills": DatasetSpec("bills", "snapshot", True, False, "Outstanding bills"),
    "vouchers": DatasetSpec(
        "vouchers",
        "transaction",
        True,
        True,
        "Vouchers from the Day Book",
        export_children=("voucher_ledger_entries", "voucher_inventory_entries"),
    ),
}


def get_dataset(name: str) -> DatasetSpec:
    key = name.strip().lower().replace("-", "_")
    try:
        return DATASET_SPECS[key]
    except KeyError as exc:
        raise ValueError(
            f"unknown dataset {name!r}; valid datasets: {', '.join(SYNC_DATASETS)}"
        ) from exc


def resolve_datasets(names: Iterable[str] | None) -> list[DatasetSpec]:
    """Return dataset specs in pipeline order for the requested names (or all)."""
    requested = {get_dataset(n).name for n in names} if names else set(SYNC_DATASETS)
    return [DATASET_SPECS[name] for name in SYNC_DATASETS if name in requested]
