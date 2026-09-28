from __future__ import annotations

from datetime import date
from decimal import Decimal

from stallion_tally.models import Company, Ledger, Voucher, VoucherLedgerEntry
from stallion_tally.sync.normalize import (
    money,
    normalize_company,
    normalize_ledger,
    normalize_voucher,
)
from stallion_tally.tally.xml_parser import parse_vouchers
from stallion_tally.utils.hashing import canonical_json, hash_record
from tests.conftest import COMPANY_1, COMPANY_1_ID, fixture_bytes


def _ledger(**overrides: object) -> Ledger:
    base = dict(
        company_id="c1",
        company_name="Co",
        name="Cash",
        parent_group="Cash-in-Hand",
        opening_balance=Decimal("10.5"),
    )
    base.update(overrides)
    return Ledger(**base)  # type: ignore[arg-type]


def test_hash_is_stable_and_field_sensitive() -> None:
    first = normalize_ledger(_ledger())
    second = normalize_ledger(_ledger())
    changed = normalize_ledger(_ledger(parent_group="Bank Accounts"))
    assert first["record_hash"] == second["record_hash"]
    assert first["record_hash"] != changed["record_hash"]
    assert len(first["record_hash"]) == 64


def test_hash_covers_raw_data() -> None:
    plain = normalize_ledger(_ledger(raw_data={"X": "1"}))
    other = normalize_ledger(_ledger(raw_data={"X": "2"}))
    assert plain["record_hash"] != other["record_hash"]


def test_money_quantizes_to_six_decimals() -> None:
    assert money(Decimal("1.23456789")) == Decimal("1.234568")
    assert money(None) is None
    assert normalize_ledger(_ledger())["opening_balance"] == Decimal("10.500000")


def test_canonical_json_is_deterministic() -> None:
    a = canonical_json({"b": Decimal("1.0"), "a": date(2026, 1, 1)})
    b = canonical_json({"a": date(2026, 1, 1), "b": Decimal("1.0")})
    assert a == b == '{"a":"2026-01-01","b":"1.0"}'
    assert hash_record({"x": 1}) == hash_record({"x": 1})


def test_normalize_company_uses_guid_as_id() -> None:
    row = normalize_company(Company(name="Acme & Co", tally_guid="guid-1"))
    assert row["company_id"] == "guid-1" and row["company_name"] == "Acme & Co"
    assert normalize_company(Company(name="Acme & Co"))["company_id"] == "acme-co"


def test_normalize_voucher_explodes_entries() -> None:
    company = Company(name=COMPANY_1, tally_guid=COMPANY_1_ID)
    vouchers = list(parse_vouchers(fixture_bytes("day_book.xml"), company))
    row, ledger_rows, inventory_rows = normalize_voucher(vouchers[0])
    assert row["tally_guid"] == vouchers[0].tally_guid
    assert row["ledger_entry_count"] == 2 and row["inventory_entry_count"] == 1
    assert [r["line_no"] for r in ledger_rows] == [1, 2]
    assert ledger_rows[0]["voucher_guid"] == row["tally_guid"]
    assert ledger_rows[0]["debit"] == Decimal("10500.000000")
    assert ledger_rows[0]["bill_allocations_json"][0]["name"] == "SI/2026/0091"
    assert inventory_rows[0]["stock_item_name"] == "Brake Pad Set"
    assert inventory_rows[0]["quantity"] == Decimal("20.000000")
    assert all("record_hash" in r for r in ledger_rows + inventory_rows)


def test_voucher_hash_changes_with_entries() -> None:
    def make(amount: str) -> Voucher:
        return Voucher(
            company_id="c",
            company_name="C",
            tally_guid="g",
            voucher_type="Journal",
            date=date(2026, 1, 1),
            ledger_entries=[VoucherLedgerEntry(line_no=1, ledger_name="A", amount=Decimal(amount))],
        )

    assert (
        normalize_voucher(make("1"))[1][0]["record_hash"]
        != normalize_voucher(make("2"))[1][0]["record_hash"]
    )
