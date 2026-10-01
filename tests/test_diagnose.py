from __future__ import annotations

from datetime import date

from lxml import etree
from sqlalchemy import select

from stallion_tally.database import Database
from stallion_tally.database.models import CompanyRow, VoucherRow
from stallion_tally.sync.diagnose import diagnose_vouchers
from stallion_tally.sync.manager import SyncManager
from stallion_tally.tally import xml_builder
from tests.conftest import COMPANY_1, COMPANY_1_ID

SALES_GUID = f"{COMPANY_1_ID}-00000101"
FROM, TO = date(2026, 1, 1), date(2026, 12, 31)


def test_probe_request_by_reserved_type() -> None:
    root = etree.fromstring(
        xml_builder.build_voucher_probe_request("Acme", FROM, TO, "Sales Order")
    )
    collection = root.find(".//COLLECTION")
    assert collection.findtext("TYPE") == "Vouchers : VoucherType"
    assert collection.findtext("CHILDOF") == "$$VchTypeSalesOrder"
    assert collection.findtext("BELONGSTO") == "Yes"
    assert collection.findtext("FILTER") == "StallionVoucherInRange"
    assert "20260101" in root.findtext(".//SYSTEM")
    assert root.findtext(".//SVCURRENTCOMPANY") == "Acme"


def test_probe_request_plain_and_custom_type() -> None:
    plain = etree.fromstring(xml_builder.build_voucher_probe_request("Acme", FROM, TO))
    assert plain.find(".//COLLECTION").findtext("TYPE") == "Voucher"
    assert plain.find(".//CHILDOF") is None
    custom = etree.fromstring(
        xml_builder.build_voucher_probe_request("Acme", FROM, TO, 'Auto "PO"')
    )
    assert custom.findtext(".//CHILDOF") == '"Auto PO"'


def _voucher_xml(guid: str, vtype: str, number: str, day: str, optional: str = "No") -> str:
    return (
        f"<VOUCHER><GUID>{guid}</GUID><VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{number}</VOUCHERNUMBER><DATE>{day}</DATE>"
        f"<ISOPTIONAL>{optional}</ISOPTIONAL></VOUCHER>"
    )


def _envelope(*vouchers: str) -> bytes:
    body = "".join(vouchers)
    return (
        f"<ENVELOPE><BODY><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>".encode()
    )


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raw_bytes(self) -> bytes:
        return self.content


class StubClient:
    """Answers probe requests from canned XML, keyed by voucher type (None = plain)."""

    def __init__(self, answers: dict[str | None, bytes]) -> None:
        self.answers = answers
        self.calls: list[str | None] = []

    def probe_vouchers(self, company, from_date, to_date, voucher_type=None, save_to=None):
        self.calls.append(voucher_type)
        return _Response(self.answers.get(voucher_type, _envelope()))


def test_diagnose_finds_hidden_orders_and_guid_collisions(
    manager: SyncManager, database: Database
) -> None:
    manager.run(export=False)
    with database.session() as s:
        sales = s.execute(
            select(VoucherRow).where(VoucherRow.tally_guid == SALES_GUID)
        ).scalar_one()
        stored_type, stored_number = sales.voucher_type, sales.voucher_number

    order = _voucher_xml("order-guid-1", "Sales Order", "SO/1", "20260915")
    same_as_db = _voucher_xml(SALES_GUID, stored_type, stored_number, "20260915")
    collision = _voucher_xml(SALES_GUID, "Journal", "JV/9", "20260915")
    client = StubClient(
        {
            None: _envelope(same_as_db),
            "Sales Order": _envelope(order),
            "Journal": _envelope(collision),
        }
    )

    with database.session() as s:
        company = s.execute(
            select(CompanyRow).where(CompanyRow.company_name == COMPANY_1)
        ).scalar_one()
        result = diagnose_vouchers(
            s,
            client,
            company,
            FROM,
            TO,
            ("Sales Order", "Journal"),  # type: ignore[arg-type]
        )

    assert client.calls == [None, "Sales Order", "Journal"]
    counts = {c.voucher_type: c for c in result.counts}
    assert counts["Sales Order"].local == 0 and counts["Sales Order"].by_type == 1
    assert counts["Sales Order"].plain_collection == 0
    assert counts[stored_type].plain_collection == 1

    missing = {(m.voucher.voucher_type, m.voucher.voucher_number): m for m in result.missing}
    assert set(missing) == {("Sales Order", "SO/1"), ("Journal", "JV/9")}
    assert missing[("Sales Order", "SO/1")].stored_as is None
    assert missing[("Sales Order", "SO/1")].found_by == "by type 'Sales Order'"
    assert stored_number in missing[("Journal", "JV/9")].stored_as
    assert SALES_GUID in result.shared_guids
