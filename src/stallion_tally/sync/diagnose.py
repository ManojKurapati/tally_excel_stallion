"""Find vouchers that Tally has but the local database does not, and why.

Asks Tally for vouchers in two ways (the plain `Voucher` collection and
`Vouchers : VoucherType` per voucher type) and compares both with what sync
stored. Read-only: nothing is written to the database.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from stallion_tally.database.models import CompanyRow, VoucherRow
from stallion_tally.tally.client import TallyClient
from stallion_tally.tally.xml_parser import ProbedVoucher, parse_voucher_probe

DEFAULT_PROBE_TYPES: tuple[str, ...] = ("Sales Order", "Purchase Order", "Journal")


@dataclass
class TypeCount:
    voucher_type: str
    local: int = 0
    plain_collection: int = 0
    by_type: int | None = None  # None = this type was not probed by type


@dataclass
class MissingVoucher:
    voucher: ProbedVoucher
    found_by: str
    stored_as: str | None  # the local voucher that already holds this GUID, if any


@dataclass
class DiagnoseResult:
    company_name: str
    from_date: date
    to_date: date
    counts: list[TypeCount] = field(default_factory=list)
    missing: list[MissingVoucher] = field(default_factory=list)
    shared_guids: dict[str, list[ProbedVoucher]] = field(default_factory=dict)
    raw_files: list[Path] = field(default_factory=list)


def _local_vouchers(
    session: Session, company_id: str, from_date: date, to_date: date
) -> list[VoucherRow]:
    return list(
        session.scalars(
            select(VoucherRow).where(
                VoucherRow.company_id == company_id,
                VoucherRow.is_deleted.is_(False),
                VoucherRow.date >= from_date,
                VoucherRow.date <= to_date,
            )
        )
    )


def _stored_as(session: Session, company_id: str, guid: str | None) -> str | None:
    if not guid:
        return None
    row = session.execute(
        select(VoucherRow).where(VoucherRow.company_id == company_id, VoucherRow.tally_guid == guid)
    ).scalar_one_or_none()
    if row is None:
        return None
    deleted = " (marked deleted)" if row.is_deleted else ""
    return f"{row.voucher_type} {row.voucher_number or '-'} dated {row.date}{deleted}"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text).strip("_").lower()


def diagnose_vouchers(
    session: Session,
    client: TallyClient,
    company: CompanyRow,
    from_date: date,
    to_date: date,
    voucher_types: tuple[str, ...] = DEFAULT_PROBE_TYPES,
    raw_dir: Path | None = None,
    now: datetime | None = None,
) -> DiagnoseResult:
    result = DiagnoseResult(company.company_name, from_date, to_date)
    if raw_dir is not None:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        raw_dir = raw_dir / f"{stamp}_{_slug(company.company_name)}"
        raw_dir.mkdir(parents=True, exist_ok=True)

    def probe(voucher_type: str | None) -> list[ProbedVoucher]:
        name = f"by_type_{_slug(voucher_type)}" if voucher_type else "plain_collection"
        path = raw_dir / f"{name}.xml" if raw_dir is not None else None
        response = client.probe_vouchers(
            company.company_name, from_date, to_date, voucher_type, save_to=path
        )
        if path is not None:
            result.raw_files.append(path)
        return parse_voucher_probe(path if path is not None else response.raw_bytes())

    local = _local_vouchers(session, company.company_id, from_date, to_date)
    # A Tally voucher is only "present" if its GUID is stored for the same voucher;
    # a GUID stored for a different voucher means one overwrote the other.
    local_by_guid = {v.tally_guid: (v.voucher_type, v.voucher_number) for v in local}

    counts: dict[str, TypeCount] = {}

    def count_for(voucher_type: str | None) -> TypeCount:
        key = voucher_type or "(no type)"
        return counts.setdefault(key, TypeCount(key))

    for v in local:
        count_for(v.voucher_type).local += 1

    probes: list[tuple[str, list[ProbedVoucher]]] = []
    plain = probe(None)
    probes.append(("plain collection", plain))
    for v in plain:
        count_for(v.voucher_type).plain_collection += 1

    for voucher_type in voucher_types:
        found = probe(voucher_type)
        probes.append((f"by type '{voucher_type}'", found))
        per_type = Counter(v.voucher_type or "(no type)" for v in found)
        if not per_type:
            count_for(voucher_type).by_type = 0
        for name, n in per_type.items():
            row = count_for(name)
            row.by_type = (row.by_type or 0) + n

    seen: set[str | None] = set()
    by_guid: dict[str, dict[tuple[str | None, str | None, date | None], ProbedVoucher]] = (
        defaultdict(dict)
    )
    for source, vouchers in probes:
        for v in vouchers:
            if v.guid:
                by_guid[v.guid][(v.voucher_type, v.voucher_number, v.date)] = v
            identity = f"{v.guid}|{v.voucher_type}|{v.voucher_number}|{v.date}"
            stored = local_by_guid.get(v.guid) if v.guid else None
            if stored == (v.voucher_type, v.voucher_number) or identity in seen:
                continue
            seen.add(identity)
            result.missing.append(
                MissingVoucher(v, source, _stored_as(session, company.company_id, v.guid))
            )

    result.shared_guids = {
        guid: list(variants.values()) for guid, variants in by_guid.items() if len(variants) > 1
    }
    result.counts = sorted(counts.values(), key=lambda c: c.voucher_type.lower())
    return result
