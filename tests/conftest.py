"""Shared test fixtures: a fake TallyPrime server, settings, database and storage."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from lxml import etree

from stallion_tally.azure.blob import LocalBlobStorage
from stallion_tally.config import Settings
from stallion_tally.database import Database
from stallion_tally.logging import configure_logging
from stallion_tally.sync.manager import SyncManager
from stallion_tally.tally.client import TallyClient

FIXTURES = Path(__file__).parent / "fixtures"
COMPANY_1 = "Stallion Automotive"
COMPANY_2 = "Stallion Parts & Service"
COMPANY_1_ID = "a1b2c3d4-0000-4000-8000-000000000001"
TODAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)

configure_logging("WARNING", "console", None, False)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeTally:
    """In-memory stand-in for TallyPrime served through `httpx.MockTransport`."""

    def __init__(self) -> None:
        self.available = True
        self.fail_next_requests = 0
        self.timeout_next_requests = 0
        self.bills_collection_error = False
        self.requests: list[dict[str, str | None]] = []
        self.companies_xml = fixture_bytes("companies.xml")
        self.masters = {
            "Groups": fixture_bytes("groups.xml"),
            "Ledgers": fixture_bytes("ledgers.xml"),
            "Stock Items": fixture_bytes("stock_items.xml"),
        }
        self.day_book_tree = etree.fromstring(
            fixture_bytes("day_book.xml"), etree.XMLParser(recover=True)
        )
        self.bills_collection = fixture_bytes("bills_collection.xml")
        self.bills_reports = {
            "Bills Receivable": fixture_bytes("bills_receivable.xml"),
            "Bills Payable": fixture_bytes("bills_payable.xml"),
        }

    # --- mutation helpers used by tests --------------------------------------
    def vouchers(self) -> list[etree._Element]:
        return list(self.day_book_tree.iter("VOUCHER"))

    def remove_voucher(self, guid: str) -> None:
        for voucher in self.vouchers():
            if voucher.findtext("GUID") == guid:
                voucher.getparent().remove(voucher)
                return
        raise KeyError(guid)

    def set_narration(self, guid: str, narration: str) -> None:
        for voucher in self.vouchers():
            if voucher.findtext("GUID") == guid:
                node = voucher.find("NARRATION")
                if node is None:
                    node = etree.SubElement(voucher, "NARRATION")
                node.text = narration
                return
        raise KeyError(guid)

    def drop_ledger_entry(self, guid: str) -> None:
        for voucher in self.vouchers():
            if voucher.findtext("GUID") == guid:
                entries = voucher.findall("ALLLEDGERENTRIES.LIST") or voucher.findall(
                    "LEDGERENTRIES.LIST"
                )
                voucher.remove(entries[-1])
                return
        raise KeyError(guid)

    # --- request handling -------------------------------------------------
    def _day_book(self, from_date: date, to_date: date) -> bytes:
        tree = copy.deepcopy(self.day_book_tree)
        for voucher in list(tree.iter("VOUCHER")):
            text = voucher.findtext("DATE") or ""
            try:
                voucher_date = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                continue
            if not from_date <= voucher_date <= to_date:
                voucher.getparent().remove(voucher)
        return etree.tostring(tree)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.available:
            raise httpx.ConnectError("connection refused", request=request)
        if self.fail_next_requests > 0:
            self.fail_next_requests -= 1
            raise httpx.ConnectError("connection reset", request=request)
        if self.timeout_next_requests > 0:
            self.timeout_next_requests -= 1
            raise httpx.ReadTimeout("read timed out", request=request)
        if request.method == "GET":
            return httpx.Response(200, content=b"<RESPONSE>TallyPrime Server is Running</RESPONSE>")

        root = etree.fromstring(request.content)
        collection_id = root.findtext("HEADER/ID")
        report = root.findtext(".//REPORTNAME")
        company = root.findtext(".//SVCURRENTCOMPANY")
        account_type = root.findtext(".//ACCOUNTTYPE")
        self.requests.append(
            {"id": collection_id, "report": report, "company": company, "type": account_type}
        )

        if collection_id == "StallionCompanies":
            return httpx.Response(200, content=self.companies_xml)
        if company not in (COMPANY_1, COMPANY_2):
            return httpx.Response(200, content=fixture_bytes("error_lineerror.xml"))
        if company == COMPANY_2:
            if collection_id == "StallionBills" or report in self.bills_reports:
                return httpx.Response(200, content=fixture_bytes("empty_collection.xml"))
            return httpx.Response(200, content=fixture_bytes("empty_masters.xml"))
        if report == "List of Accounts":
            return httpx.Response(200, content=self.masters[account_type])
        if collection_id == "StallionVouchers":
            from_text = root.findtext(".//SVFROMDATE")
            to_text = root.findtext(".//SVTODATE")
            from_date = date(int(from_text[:4]), int(from_text[4:6]), int(from_text[6:]))
            to_date = date(int(to_text[:4]), int(to_text[4:6]), int(to_text[6:]))
            return httpx.Response(200, content=self._day_book(from_date, to_date))
        if collection_id == "StallionBills":
            if self.bills_collection_error:
                return httpx.Response(200, content=fixture_bytes("error_lineerror.xml"))
            return httpx.Response(200, content=self.bills_collection)
        if report in self.bills_reports:
            return httpx.Response(200, content=self.bills_reports[report])
        return httpx.Response(200, content=fixture_bytes("error_response.xml"))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_tally() -> FakeTally:
    return FakeTally()


@pytest.fixture
def tally_client(fake_tally: FakeTally) -> Iterator[TallyClient]:
    client = TallyClient(
        "http://localhost:9000",
        retry_attempts=3,
        backoff_schedule=(0, 0, 0),
        transport=fake_tally.transport(),
        sleep=lambda _s: None,
    )
    yield client
    client.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="development",
        app_data_dir=tmp_path / "data",
        azure_storage_backend="local",
        azure_local_backend_dir=tmp_path / "cloud",
        tally_voucher_chunk_days=7,
        tally_voucher_chunk_size=2,
        export_batch_size=3,
        retry_max_attempts=3,
        retry_backoff_seconds="0,0,0",
        log_to_file=False,
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    settings.ensure_directories()
    db = Database(settings.resolved_database_url)
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def storage(settings: Settings) -> LocalBlobStorage:
    return LocalBlobStorage(settings.local_backend_dir / settings.azure_storage_container)


@pytest.fixture
def clock() -> object:
    class Clock:
        def __init__(self) -> None:
            self.now = NOW

        def __call__(self) -> datetime:
            return self.now

        def advance(self, **kwargs: int) -> None:
            from datetime import timedelta

            self.now = self.now + timedelta(**kwargs)

    return Clock()


@pytest.fixture
def manager(
    settings: Settings,
    database: Database,
    tally_client: TallyClient,
    storage: LocalBlobStorage,
    clock: object,
) -> SyncManager:
    return SyncManager(settings, database, tally_client, storage, clock=clock)  # type: ignore[arg-type]
