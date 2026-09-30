"""Python objects -> Tally XML requests.

Every request the application sends to TallyPrime is produced here so the
request formats live in one place. Values are escaped by lxml.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from lxml import etree

from stallion_tally.utils.dates import to_tally_date

XML_EXPORT_FORMAT = "$$SysName:XML"

COMPANY_FETCH_FIELDS: tuple[str, ...] = (
    "NAME",
    "GUID",
    "STARTINGFROM",
    "BOOKSFROM",
    "ENDINGAT",
    "BASICCOMPANYFORMALNAME",
    "MAILINGNAME",
    "COMPANYNUMBER",
    "ALTERID",
    "MASTERID",
    "EMAIL",
    "STATENAME",
    "COUNTRYNAME",
    "PINCODE",
    "ADDRESS",
    "BASECURRENCYSYMBOL",
    "BASECURRENCYNAME",
)

BILL_FETCH_FIELDS: tuple[str, ...] = (
    "NAME",
    "PARENT",
    "BILLDATE",
    "BILLCREDITPERIOD",
    "OPENINGBALANCE",
    "CLOSINGBALANCE",
    "FINALDUEDATE",
    "OVERDUEDAYS",
    "ISADVANCE",
    "BILLTYPE",
)

# Vouchers are exported through a TDL collection rather than the Day Book
# report: TallyPrime ignores SVFROMDATE/SVTODATE on a Day Book export (it only
# returns the current date), and the Day Book's ledger entries omit the
# sales/purchase ledger of invoice-mode vouchers.
VOUCHER_FETCH_FIELDS: tuple[str, ...] = (
    "*",
    "AllLedgerEntries",
    "AllInventoryEntries",
    "LedgerEntries",
    "InventoryEntries",
)

# Voucher date as a YYYYMMDD number. Compared numerically so the filter does
# not depend on how Tally parses date strings under the machine's locale
# (`$$Date:"..."` literals and ##SVFromDate both evaluated to empty dates).
_VOUCHER_DATE_KEY = "(($$YearOfDate:$Date * 10000) + ($$MonthOfDate:$Date * 100) + $$DayOfDate:$Date)"

MASTER_ACCOUNT_TYPES: dict[str, str] = {
    "groups": "Groups",
    "ledgers": "Ledgers",
    "stock_items": "Stock Items",
}


def _sub(parent: etree._Element, tag: str, text: str | None = None) -> etree._Element:
    element = etree.SubElement(parent, tag)
    if text is not None:
        element.text = text
    return element


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _static_variables(
    parent: etree._Element,
    company: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> etree._Element:
    static = _sub(parent, "STATICVARIABLES")
    _sub(static, "SVEXPORTFORMAT", XML_EXPORT_FORMAT)
    if company:
        _sub(static, "SVCURRENTCOMPANY", company)
    for key, value in (extra or {}).items():
        _sub(static, key, value)
    return static


def build_collection_request(
    collection_name: str,
    object_type: str,
    fetch: Iterable[str],
    *,
    company: str | None = None,
    static_variables: Mapping[str, str] | None = None,
    filters: Mapping[str, str] | None = None,
    native_method: str | None = None,
) -> bytes:
    """Build a TDL Collection export request.

    `filters` maps formula names to TDL formula expressions; each formula is
    declared as a SYSTEM formula and applied to the collection.
    """
    root = etree.Element("ENVELOPE")
    header = _sub(root, "HEADER")
    _sub(header, "VERSION", "1")
    _sub(header, "TALLYREQUEST", "Export")
    _sub(header, "TYPE", "Collection")
    _sub(header, "ID", collection_name)

    body = _sub(root, "BODY")
    desc = _sub(body, "DESC")
    _static_variables(desc, company, static_variables)
    tdl = _sub(desc, "TDL")
    message = _sub(tdl, "TDLMESSAGE")
    collection = etree.SubElement(
        message,
        "COLLECTION",
        NAME=collection_name,
        ISMODIFY="No",
        ISFIXED="No",
        ISINITIALIZE="No",
        ISOPTION="No",
        ISINTERNAL="No",
    )
    _sub(collection, "TYPE", object_type)
    fetch_list = list(fetch)
    if fetch_list:
        _sub(collection, "FETCH", ", ".join(fetch_list))
    if native_method:
        _sub(collection, "NATIVEMETHOD", native_method)
    for name in filters or {}:
        _sub(collection, "FILTER", name)
    for name, formula in (filters or {}).items():
        system = etree.SubElement(message, "SYSTEM", TYPE="Formulae", NAME=name)
        system.text = formula
    return _serialize(root)


def build_report_export_request(
    report_name: str,
    *,
    company: str | None = None,
    static_variables: Mapping[str, str] | None = None,
) -> bytes:
    """Build an `Export Data` request for a built-in Tally report."""
    root = etree.Element("ENVELOPE")
    header = _sub(root, "HEADER")
    _sub(header, "TALLYREQUEST", "Export Data")
    body = _sub(root, "BODY")
    export = _sub(body, "EXPORTDATA")
    request = _sub(export, "REQUESTDESC")
    _sub(request, "REPORTNAME", report_name)
    _static_variables(request, company, static_variables)
    return _serialize(root)


# ---------------------------------------------------------------------------
# Dataset specific requests
# ---------------------------------------------------------------------------


def build_companies_request() -> bytes:
    """List the companies currently loaded in Tally."""
    return build_collection_request("StallionCompanies", "Company", COMPANY_FETCH_FIELDS)


def build_masters_request(company: str, dataset: str) -> bytes:
    """Export all masters of one type (groups, ledgers, stock items)."""
    try:
        account_type = MASTER_ACCOUNT_TYPES[dataset]
    except KeyError as exc:
        raise ValueError(f"no master export for dataset {dataset!r}") from exc
    return build_report_export_request(
        "List of Accounts",
        company=company,
        static_variables={"ACCOUNTTYPE": account_type},
    )


def build_day_book_request(company: str, from_date: date, to_date: date) -> bytes:
    """Export all vouchers dated within a range (inclusive).

    See `VOUCHER_FETCH_FIELDS` for why this is a Voucher collection and not
    the Day Book report. SVFROMDATE/SVTODATE are still sent; Tally does not
    rely on them here, the date filter formula does the selection.
    """
    if to_date < from_date:
        raise ValueError("to_date must not be before from_date")
    from_key, to_key = to_tally_date(from_date), to_tally_date(to_date)
    return build_collection_request(
        "StallionVouchers",
        "Voucher",
        VOUCHER_FETCH_FIELDS,
        company=company,
        static_variables={"SVFROMDATE": from_key, "SVTODATE": to_key},
        filters={
            "StallionVoucherInRange": (
                f"{_VOUCHER_DATE_KEY} >= {from_key} AND {_VOUCHER_DATE_KEY} <= {to_key}"
            )
        },
        native_method="*",
    )


def build_bills_collection_request(company: str) -> bytes:
    """Outstanding bills through a TDL collection over the `Bills` object."""
    return build_collection_request(
        "StallionBills",
        "Bills",
        BILL_FETCH_FIELDS,
        company=company,
        filters={"StallionPendingBill": "NOT $$IsEmpty:$ClosingBalance"},
    )


def build_bills_report_request(company: str, receivable: bool) -> bytes:
    """Outstanding bills through the built-in Bills Receivable/Payable reports."""
    return build_report_export_request(
        "Bills Receivable" if receivable else "Bills Payable",
        company=company,
    )
