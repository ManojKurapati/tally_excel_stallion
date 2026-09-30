from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from stallion_tally.models import Company
from stallion_tally.tally.exceptions import TallyParseError, TallyResponseError
from stallion_tally.tally.xml_parser import (
    ParseReport,
    SanitizingReader,
    decode_tally_bytes,
    detect_encoding,
    element_to_dict,
    iter_elements,
    parse_amount,
    parse_bills_collection,
    parse_bills_report,
    parse_companies,
    parse_groups,
    parse_ledgers,
    parse_quantity,
    parse_rate,
    parse_root,
    parse_stock_items,
    parse_tally_date,
    parse_vouchers,
    raise_for_tally_error,
    sanitize_text,
)
from tests.conftest import COMPANY_1, COMPANY_1_ID, fixture_bytes

COMPANY = Company(name=COMPANY_1, tally_guid=COMPANY_1_ID)


# --- value parsers -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("-1000.00", Decimal("-1000.00")),
        ("1,00,000.50", Decimal("100000.50")),
        (" 250", Decimal("250")),
        ("(300.00)", Decimal("-300.00")),
        ("1000.00 Dr", Decimal("-1000.00")),
        ("1000.00 Cr", Decimal("1000.00")),
        ("-$ 100.00 @ 3.6725/$ = -367.25", Decimal("-367.25")),
        ("$ 100.00 @ 3.6725/$ = 367.25", Decimal("367.25")),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_amount(text: str | None, expected: Decimal | None) -> None:
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (" 20 Nos", (Decimal("20"), "Nos")),
        ("-2.500 Kgs", (Decimal("-2.500"), "Kgs")),
        (" 120.500 Ltr = 12 Box", (Decimal("120.500"), "Ltr")),
        ("1,000 Pcs", (Decimal("1000"), "Pcs")),
        ("", (None, None)),
    ],
)
def test_parse_quantity(text: str, expected: tuple) -> None:
    assert parse_quantity(text) == expected


def test_parse_rate() -> None:
    assert parse_rate("500.00/Nos") == (Decimal("500.00"), "Nos")
    assert parse_rate("12.5") == (Decimal("12.5"), None)
    assert parse_rate(None) == (None, None)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20260915", date(2026, 9, 15)),
        ("2026-09-15", date(2026, 9, 15)),
        ("15-Sep-26", date(2026, 9, 15)),
        ("1-Jun-2026", date(2026, 6, 1)),
        ("15/09/2026", date(2026, 9, 15)),
        ("20261345", None),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_tally_date(text: str, expected: date | None) -> None:
    assert parse_tally_date(text) == expected


# --- sanitising / decoding ---------------------------------------------------


def test_sanitize_removes_control_chars_and_refs() -> None:
    assert sanitize_text("a\x04b&#4;c&#x1;d&#65;e") == "a b c d&#65;e"
    assert sanitize_text("tab\tok\nline") == "tab\tok\nline"


def test_detect_encoding_variants() -> None:
    assert detect_encoding(b"\xef\xbb\xbf<ENVELOPE/>") == "utf-8-sig"
    assert detect_encoding("<ENVELOPE/>".encode("utf-16")) == "utf-16"
    assert detect_encoding("<ENVELOPE/>".encode("utf-16-le")) == "utf-16-le"
    assert detect_encoding(b'<?xml version="1.0" encoding="ISO-8859-1"?><E/>') == "iso-8859-1"
    assert detect_encoding(b"<ENVELOPE/>") == "utf-8"


def test_decode_utf16_response_with_prolog() -> None:
    data = '<?xml version="1.0" encoding="UTF-16"?>\n<ENVELOPE><X>café</X></ENVELOPE>'.encode(
        "utf-16"
    )
    text = decode_tally_bytes(data)
    assert text.lstrip().startswith("<ENVELOPE>")
    assert parse_root(data).findtext("X") == "café"


def test_sanitizing_reader_streams_in_small_chunks() -> None:
    body = b'<?xml version="1.0" encoding="UTF-8"?><ENVELOPE><A>x&#4;y\x01z</A></ENVELOPE>'
    import io

    reader = SanitizingReader(io.BytesIO(body), chunk_size=3)
    out = b""
    while chunk := reader.read(5):
        out += chunk
    assert out == b"<ENVELOPE><A>x y z</A></ENVELOPE>"


def test_iter_elements_requires_envelope() -> None:
    with pytest.raises(TallyParseError):
        list(iter_elements(b"<html><body>nope</body></html>", "VOUCHER"))


def test_iter_elements_handles_empty_bytes() -> None:
    with pytest.raises(TallyParseError):
        list(iter_elements(b"", "VOUCHER"))


# --- error detection ---------------------------------------------------------


def test_lineerror_raises_response_error() -> None:
    with pytest.raises(TallyResponseError, match="not loaded"):
        raise_for_tally_error(fixture_bytes("error_lineerror.xml"))


def test_unknown_request_raises_response_error() -> None:
    with pytest.raises(TallyResponseError, match="Unknown Request"):
        raise_for_tally_error(fixture_bytes("error_response.xml"))


def test_server_running_response_is_not_an_error() -> None:
    raise_for_tally_error(b"<RESPONSE>TallyPrime Server is Running</RESPONSE>")


def test_valid_data_passes_error_check() -> None:
    raise_for_tally_error(fixture_bytes("ledgers.xml"))


def test_non_xml_raises_parse_error() -> None:
    with pytest.raises(TallyParseError):
        raise_for_tally_error(b"")


# --- dataset parsers ---------------------------------------------------------


def test_parse_companies() -> None:
    report = ParseReport()
    companies = parse_companies(fixture_bytes("companies.xml"), report)
    assert report.parsed == 2 and report.rejected == 0
    first, second = companies
    assert first.name == COMPANY_1
    assert first.company_id == COMPANY_1_ID
    assert first.books_from == date(2025, 4, 1)
    assert first.base_currency_symbol == "AED"
    assert first.alter_id == 1234
    assert first.address == "Al Quoz Industrial Area 3\nDubai"
    assert first.pincode is None
    assert second.name == "Stallion Parts & Service"
    assert first.raw_data["GUID"] == COMPANY_1_ID


def test_parse_groups() -> None:
    report = ParseReport()
    groups = list(parse_groups(fixture_bytes("groups.xml"), COMPANY, report))
    assert [g.name for g in groups] == ["Sundry Debtors", "Sales Accounts", "Workshop Customers"]
    sales = groups[1]
    assert sales.parent is None
    assert sales.is_revenue is True and sales.affects_gross_profit is True
    assert sales.alias == "Sales"
    assert sales.sort_position == 50
    assert groups[0].company_id == COMPANY_1_ID


def test_parse_ledgers_from_file(tmp_path: Path) -> None:
    path = tmp_path / "ledgers.xml"
    path.write_bytes(fixture_bytes("ledgers.xml"))
    report = ParseReport()
    ledgers = list(parse_ledgers(path, COMPANY, report))
    assert report.parsed == 3
    assert report.rejected == 1  # the ledger without a name
    by_name = {ledger.name: ledger for ledger in ledgers}
    cash = by_name["Cash"]
    assert cash.opening_balance == Decimal("-15000.00")
    assert cash.parent_group == "Cash-in-Hand"
    assert cash.tally_guid.endswith("-00000003")
    afm = by_name["Al Futtaim Motors"]
    assert afm.alias == "AFM"
    assert afm.address == "Sheikh Zayed Road\nDubai"
    assert afm.state == "Dubai" and afm.country == "United Arab Emirates"
    assert afm.gstin == "100123456700003" and afm.gst_registration_type == "Regular"
    assert afm.credit_limit == Decimal("100000.00")
    assert afm.credit_period == "30 Days"
    assert afm.mailing_name == "Al Futtaim Motors LLC"
    assert afm.pan == "ABCDE1234F"
    sales = by_name["Sales - Parts"]
    assert sales.opening_balance is None
    assert sales.raw_data["DESCRIPTION"] == "Parts Counter sales"
    assert sales.raw_data["UDF:CUSTOMCODE.LIST"][0]["UDF:CUSTOMCODE"] == "SP-01"


def test_parse_stock_items() -> None:
    report = ParseReport()
    items = list(parse_stock_items(fixture_bytes("stock_items.xml"), COMPANY, report))
    assert report.parsed == 2
    brake, oil = items
    assert brake.opening_quantity == Decimal("40") and brake.base_unit == "Nos"
    assert brake.opening_value == Decimal("-12000.00")
    assert brake.opening_rate == Decimal("300.00")
    assert brake.hsn_code == "87083010"
    assert brake.part_number == "BP-4471"
    assert brake.gst_applicable == "Applicable"
    assert oil.opening_quantity == Decimal("120.500") and oil.base_unit == "Ltr"
    assert oil.additional_unit == "Box"


def test_parse_vouchers() -> None:
    report = ParseReport()
    vouchers = list(parse_vouchers(fixture_bytes("day_book.xml"), COMPANY, report))
    assert report.parsed == 5
    assert report.rejected == 1  # the voucher without a GUID
    assert "BROKEN-NO-GUID" in report.errors[0]
    sales = vouchers[0]
    assert sales.tally_guid.endswith("-00000101")
    assert sales.voucher_type == "Sales" and sales.voucher_number == "SI/2026/0091"
    assert sales.date == date(2026, 9, 15)
    assert sales.party_ledger_name == "Al Futtaim Motors"
    assert sales.narration == "Being sale of brake pads Counter"
    assert sales.is_invoice is True and sales.is_cancelled is False
    assert sales.alter_id == 2301
    assert sales.persisted_view == "Invoice Voucher View"
    assert [e.ledger_name for e in sales.ledger_entries] == ["Al Futtaim Motors", "VAT Output 5%"]
    party = sales.ledger_entries[0]
    assert (
        party.amount == Decimal("-10500.00")
        and party.debit == Decimal("10500.00")
        and party.credit is None
    )
    assert party.is_party_ledger is True
    assert party.bill_allocations[0].name == "SI/2026/0091"
    assert party.bill_allocations[0].bill_type == "New Ref"
    assert sales.total_debit == Decimal("10500.00") and sales.total_credit == Decimal("500.00")
    inventory = sales.inventory_entries[0]
    assert inventory.stock_item_name == "Brake Pad Set"
    assert inventory.quantity == Decimal("20") and inventory.unit == "Nos"
    assert inventory.rate == Decimal("500.00") and inventory.rate_unit == "Nos"
    assert inventory.amount == Decimal("10000.00")
    assert inventory.godown_name == "Main Location" and inventory.batch_name == "Primary Batch"
    assert inventory.accounting_ledger == "Sales - Parts"
    assert sales.raw_data["UDF:DELIVERYNOTE.LIST"][0]["UDF:DELIVERYNOTE"] == "DN-88"
    assert sales.raw_data["@VCHTYPE"] == "Sales"

    payment = vouchers[1]
    assert payment.ledger_entries[0].amount == Decimal("-367.25")
    assert payment.ledger_entries[0].bill_allocations[0].amount == Decimal("-367.25")
    assert (
        payment.ledger_entries[1].extra["BANKALLOCATIONS.LIST"][0]["TRANSACTIONTYPE"]
        == "e-Fund Transfer"
    )
    assert payment.total_debit == payment.total_credit == Decimal("367.25")

    cancelled = vouchers[2]
    assert cancelled.is_cancelled is True and cancelled.ledger_entries == []

    journal = vouchers[3]
    assert journal.ledger_entries[0].debit == Decimal("2500.00")


def test_parse_bills_collection() -> None:
    report = ParseReport()
    bills = parse_bills_collection(fixture_bytes("bills_collection.xml"), COMPANY, report)
    assert report.parsed == 2
    receivable, payable = bills
    assert receivable.ledger_name == "Al Futtaim Motors" and receivable.bill_ref == "SI/2026/0091"
    assert receivable.bill_type == "receivable"
    assert receivable.closing_amount == Decimal("-10500.00")
    assert receivable.bill_date == date(2026, 9, 15) and receivable.due_date == date(2026, 10, 15)
    assert receivable.credit_period == "30 Days" and receivable.overdue_days == 0
    assert payable.bill_type == "payable" and payable.overdue_days == 13


def test_parse_bills_collection_undeclared_udf_prefix() -> None:
    # Collection exports use the UDF: prefix without declaring its namespace.
    xml = b"""<ENVELOPE><BODY><DATA><COLLECTION>
     <BILL NAME="INV-1 - WHT">
      <NAME>INV-1 - WHT</NAME><PARENT>Party A</PARENT>
      <CLOSINGBALANCE TYPE="Amount">-1510500.00</CLOSINGBALANCE>
      <UDF:_UDF_788551165.LIST DESC="" ISLIST="YES" TYPE="String" INDEX="22012">
       <UDF:_UDF_788551165 DESC="">ABUJA-HMNL</UDF:_UDF_788551165>
      </UDF:_UDF_788551165.LIST>
     </BILL>
    </COLLECTION></DATA></BODY></ENVELOPE>"""
    report = ParseReport()
    bills = parse_bills_collection(xml, COMPANY, report)
    assert report.rejected == 0 and len(bills) == 1
    assert bills[0].raw_data["UDF:_UDF_788551165.LIST"] == [
        {"@DESC": "", "@ISLIST": "YES", "@TYPE": "String", "@INDEX": "22012",
         "UDF:_UDF_788551165": "ABUJA-HMNL"}
    ]  # fmt: skip


def test_parse_bills_collection_empty() -> None:
    assert parse_bills_collection(fixture_bytes("empty_collection.xml"), COMPANY) == []


def test_parse_bills_report() -> None:
    report = ParseReport()
    bills = parse_bills_report(fixture_bytes("bills_receivable.xml"), COMPANY, "receivable", report)
    assert report.parsed == 2
    assert bills[0].bill_ref == "SI/2026/0091" and bills[0].ledger_name == "Al Futtaim Motors"
    assert bills[0].bill_date == date(2026, 9, 15) and bills[0].due_date == date(2026, 10, 15)
    assert bills[0].closing_amount == Decimal("10500.00")
    assert bills[1].closing_amount == Decimal("1250.00") and bills[1].overdue_days == 89
    assert bills[1].bill_date == date(2026, 6, 1)


def test_element_to_dict_lists_and_attributes() -> None:
    root = parse_root(
        b"<ENVELOPE><V A='1'><X>1</X><X>2</X><L.LIST><N>a</N></L.LIST><E/></V></ENVELOPE>"
    )
    data = element_to_dict(root.find("V"))
    assert data == {"@A": "1", "X": ["1", "2"], "L.LIST": [{"N": "a"}], "E": None}
