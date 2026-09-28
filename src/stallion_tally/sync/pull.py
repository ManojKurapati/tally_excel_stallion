"""Pull datasets from Tally: build request, save the raw response, stream-parse it."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from stallion_tally.logging import get_logger
from stallion_tally.models import Bill, Company, Group, Ledger, StockItem, Voucher
from stallion_tally.tally.client import TallyClient, TallyResponse
from stallion_tally.tally.exceptions import TallyParseError, TallyResponseError
from stallion_tally.tally.xml_parser import (
    ParseReport,
    parse_bills_collection,
    parse_bills_report,
    parse_companies,
    parse_groups,
    parse_ledgers,
    parse_stock_items,
    parse_vouchers,
)
from stallion_tally.utils.dates import utcnow
from stallion_tally.utils.hashing import sha256_file
from stallion_tally.utils.text import safe_name

log = get_logger(__name__)

MASTER_PARSERS: dict[str, Callable[..., Iterator[Any]]] = {
    "groups": parse_groups,
    "ledgers": parse_ledgers,
    "stock_items": parse_stock_items,
}


@dataclass(frozen=True)
class RawFile:
    """A raw Tally response preserved on disk."""

    dataset: str
    company: Company | None
    path: Path
    size_bytes: int
    checksum_sha256: str
    day: str

    @property
    def blob_path(self) -> str:
        company = safe_name(self.company.name) if self.company else "_all"
        return f"raw/{company}/{self.dataset}/{self.day}/{self.path.name}"


class Puller:
    """Requests datasets from Tally and preserves the raw responses."""

    def __init__(
        self,
        client: TallyClient,
        raw_dir: Path,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.raw_dir = raw_dir
        self.clock = clock

    # ------------------------------------------------------------------ raw files
    def _raw_path(
        self, dataset: str, company: Company | None, seq: int | None = None
    ) -> tuple[Path, str]:
        now = self.clock()
        day = now.date().isoformat()
        folder = self.raw_dir / day / (safe_name(company.name) if company else "_all")
        suffix = f"_{seq:03d}" if seq is not None else ""
        return folder / f"{dataset}_{now.strftime('%H%M%S')}{suffix}.xml", day

    def _raw_file(
        self, dataset: str, company: Company | None, response: TallyResponse, day: str
    ) -> RawFile:
        path = response.path
        assert path is not None
        return RawFile(
            dataset=dataset,
            company=company,
            path=path,
            size_bytes=response.size_bytes,
            checksum_sha256=sha256_file(path),
            day=day,
        )

    # ------------------------------------------------------------------ datasets
    def pull_companies(self) -> tuple[list[Company], RawFile, ParseReport]:
        path, day = self._raw_path("companies", None)
        response = self.client.get_companies(save_to=path)
        report = ParseReport()
        companies = parse_companies(path, report)
        return companies, self._raw_file("companies", None, response, day), report

    def pull_masters(
        self, company: Company, dataset: str
    ) -> tuple[Iterator[Group | Ledger | StockItem], RawFile, ParseReport]:
        parser = MASTER_PARSERS[dataset]
        path, day = self._raw_path(dataset, company)
        response = self.client.get_masters(company.name, dataset, save_to=path)
        report = ParseReport()
        return (
            parser(path, company, report),
            self._raw_file(dataset, company, response, day),
            report,
        )

    def pull_day_book(
        self, company: Company, from_date: date, to_date: date, seq: int
    ) -> tuple[Iterator[Voucher], RawFile, ParseReport]:
        path, day = self._raw_path("vouchers", company, seq)
        response = self.client.get_day_book(company.name, from_date, to_date, save_to=path)
        report = ParseReport()
        return (
            parse_vouchers(path, company, report),
            self._raw_file("vouchers", company, response, day),
            report,
        )

    def pull_bills(self, company: Company) -> tuple[list[Bill], list[RawFile], ParseReport]:
        """Outstanding bills: TDL collection first, built-in reports as fallback."""
        report = ParseReport()
        raw_files: list[RawFile] = []
        path, day = self._raw_path("bills", company, 1)
        try:
            response = self.client.get_bills(company.name, save_to=path)
            raw_files.append(self._raw_file("bills", company, response, day))
            bills = parse_bills_collection(path, company, report)
            if bills:
                return bills, raw_files, report
            log.info(
                "Bills collection returned no records, trying Bills Receivable/Payable reports",
                company=company.name,
            )
        except (TallyResponseError, TallyParseError) as exc:
            log.warning(
                "Bills collection request failed, trying Bills Receivable/Payable reports",
                company=company.name,
                error=str(exc),
            )
        report = ParseReport()
        bills = []
        for seq, (receivable, bill_type) in enumerate(
            ((True, "receivable"), (False, "payable")), start=2
        ):
            path, day = self._raw_path("bills", company, seq)
            response = self.client.get_bills_report(company.name, receivable, save_to=path)
            raw_files.append(self._raw_file("bills", company, response, day))
            bills.extend(parse_bills_report(path, company, bill_type, report))
        return bills, raw_files, report
