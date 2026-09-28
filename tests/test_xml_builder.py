from __future__ import annotations

from datetime import date

import pytest
from lxml import etree

from stallion_tally.tally import xml_builder


def _root(xml: bytes) -> etree._Element:
    return etree.fromstring(xml)


def test_companies_request_is_a_company_collection() -> None:
    root = _root(xml_builder.build_companies_request())
    assert root.findtext("HEADER/TALLYREQUEST") == "Export"
    assert root.findtext("HEADER/TYPE") == "Collection"
    collection = root.find(".//COLLECTION")
    assert collection.get("NAME") == root.findtext("HEADER/ID")
    assert collection.findtext("TYPE") == "Company"
    assert "GUID" in collection.findtext("FETCH")
    assert root.findtext(".//SVEXPORTFORMAT") == "$$SysName:XML"


def test_masters_request_sets_company_and_account_type() -> None:
    root = _root(xml_builder.build_masters_request("Stallion & Co", "ledgers"))
    assert root.findtext("HEADER/TALLYREQUEST") == "Export Data"
    assert root.findtext(".//REPORTNAME") == "List of Accounts"
    assert root.findtext(".//SVCURRENTCOMPANY") == "Stallion & Co"
    assert root.findtext(".//ACCOUNTTYPE") == "Ledgers"
    assert b"Stallion &amp; Co" in xml_builder.build_masters_request("Stallion & Co", "ledgers")


@pytest.mark.parametrize("dataset,expected", [("groups", "Groups"), ("stock_items", "Stock Items")])
def test_master_account_types(dataset: str, expected: str) -> None:
    root = _root(xml_builder.build_masters_request("X", dataset))
    assert root.findtext(".//ACCOUNTTYPE") == expected


def test_unknown_master_dataset_rejected() -> None:
    with pytest.raises(ValueError):
        xml_builder.build_masters_request("X", "vouchers")


def test_day_book_request_dates() -> None:
    root = _root(xml_builder.build_day_book_request("X", date(2026, 1, 5), date(2026, 1, 31)))
    assert root.findtext(".//REPORTNAME") == "Day Book"
    assert root.findtext(".//SVFROMDATE") == "20260105"
    assert root.findtext(".//SVTODATE") == "20260131"


def test_day_book_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        xml_builder.build_day_book_request("X", date(2026, 2, 1), date(2026, 1, 1))


def test_bills_collection_request_has_filter_formula() -> None:
    root = _root(xml_builder.build_bills_collection_request("X"))
    collection = root.find(".//COLLECTION")
    assert collection.findtext("TYPE") == "Bills"
    filter_name = collection.findtext("FILTER")
    system = root.find(f".//SYSTEM[@NAME='{filter_name}']")
    assert system is not None and system.get("TYPE") == "Formulae"
    assert "ClosingBalance" in system.text


def test_bills_report_requests() -> None:
    assert (
        _root(xml_builder.build_bills_report_request("X", True)).findtext(".//REPORTNAME")
        == "Bills Receivable"
    )
    assert (
        _root(xml_builder.build_bills_report_request("X", False)).findtext(".//REPORTNAME")
        == "Bills Payable"
    )
