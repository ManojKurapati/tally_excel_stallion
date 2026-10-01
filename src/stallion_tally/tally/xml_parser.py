"""Tally XML -> validated Python models.

Tally responses are frequently not well-formed XML (control characters,
odd encodings, undefined entities). Everything here goes through a
sanitising reader and lxml's recovering parser, and large responses are
streamed element by element rather than loaded into a DOM.
"""

from __future__ import annotations

import codecs
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO

from lxml import etree
from pydantic import ValidationError

from stallion_tally.models import (
    Bill,
    BillAllocation,
    Company,
    Group,
    Ledger,
    StockItem,
    Voucher,
    VoucherInventoryEntry,
    VoucherLedgerEntry,
)
from stallion_tally.tally.exceptions import TallyParseError, TallyResponseError
from stallion_tally.utils.text import clean_text

_PROLOG_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_ENCODING_RE = re.compile(rb"<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", re.IGNORECASE)
_INVALID_XML_CHARS = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")
_CHAR_REF_RE = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")
_ERROR_SCAN_LIMIT = 256 * 1024

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip


# ---------------------------------------------------------------------------
# Decoding and sanitising
# ---------------------------------------------------------------------------


def _is_valid_xml_char(code: int) -> bool:
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _replace_char_ref(match: re.Match[str]) -> str:
    ref = match.group(1)
    code = int(ref[1:], 16) if ref[0] in "xX" else int(ref)
    return match.group(0) if _is_valid_xml_char(code) else " "


def sanitize_text(text: str) -> str:
    """Remove characters and character references that XML 1.0 forbids."""
    text = _CHAR_REF_RE.sub(_replace_char_ref, text)
    return _INVALID_XML_CHARS.sub(" ", text)


def detect_encoding(head: bytes) -> str:
    """Guess the encoding of a Tally response from its first bytes."""
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if len(head) >= 4 and head[0:1] != b"\x00" and head[1:2] == b"\x00" and head[3:4] == b"\x00":
        return "utf-16-le"
    if len(head) >= 4 and head[0:1] == b"\x00" and head[2:3] == b"\x00":
        return "utf-16-be"
    match = _ENCODING_RE.search(head[:200])
    if match:
        declared = match.group(1).decode("ascii", "ignore").lower()
        try:
            codecs.lookup(declared)
            return declared
        except LookupError:
            pass
    return "utf-8"


def decode_tally_bytes(data: bytes) -> str:
    """Decode a complete Tally response to text (without the XML prolog)."""
    text = data.decode(detect_encoding(data[:512]), errors="replace")
    return _PROLOG_RE.sub("", text, count=1)


def sanitize_bytes(data: bytes) -> bytes:
    """Decode, sanitise and re-encode a complete response as UTF-8 bytes."""
    return sanitize_text(decode_tally_bytes(data)).encode("utf-8")


class SanitizingReader:
    """File-like reader that yields sanitised UTF-8 bytes from a raw response.

    Decoding is incremental so arbitrarily large responses are processed
    with bounded memory. Suitable as the source for `lxml.etree.iterparse`.
    """

    def __init__(self, stream: BinaryIO, chunk_size: int = 64 * 1024) -> None:
        self._stream = stream
        self._chunk_size = chunk_size
        self._decoder: codecs.IncrementalDecoder | None = None
        self._pending = ""
        self._buffer = b""
        self._eof = False
        self._first = True
        self._head = ""

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._buffer) < size):
            self._fill()
        if size < 0:
            out, self._buffer = self._buffer, b""
        else:
            out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def _fill(self) -> None:
        raw = self._stream.read(self._chunk_size)
        if self._decoder is None:
            self._decoder = codecs.getincrementaldecoder(detect_encoding(raw[:512]))(
                errors="replace"
            )
        final = not raw
        text = self._pending + self._decoder.decode(raw, final=final)
        if final:
            self._eof = True
            self._pending = ""
        else:
            # Hold back a possibly incomplete character reference (e.g. "&#1").
            amp = text.rfind("&")
            if amp != -1 and amp >= len(text) - 12 and ";" not in text[amp:]:
                self._pending, text = text[amp:], text[:amp]
            else:
                self._pending = ""
        if self._first:
            # Wait until the XML prolog (if any) is complete before stripping it.
            self._head += text
            stripped = self._head.lstrip()
            could_be_prolog = stripped.startswith("<?xml") or "<?xml".startswith(stripped[:5])
            if not final and could_be_prolog and "?>" not in stripped and len(self._head) < 4096:
                return
            text = _PROLOG_RE.sub("", self._head, count=1)
            self._head = ""
            self._first = False
        self._buffer += sanitize_text(text).encode("utf-8")

    def close(self) -> None:
        self._stream.close()


def open_sanitized(source: bytes | Path) -> SanitizingReader:
    stream: BinaryIO = io.BytesIO(source) if isinstance(source, bytes) else source.open("rb")
    return SanitizingReader(stream)


def _source_size(source: bytes | Path) -> int:
    return len(source) if isinstance(source, bytes) else source.stat().st_size


# ---------------------------------------------------------------------------
# Generic parsing helpers
# ---------------------------------------------------------------------------


def parse_root(source: bytes | Path) -> etree._Element:
    """Parse a complete response into an element tree (for small responses)."""
    data = source if isinstance(source, bytes) else source.read_bytes()
    parser = etree.XMLParser(recover=True, huge_tree=True, remove_blank_text=True)
    try:
        root = etree.fromstring(sanitize_bytes(data), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise TallyParseError(f"response is not valid XML: {exc}") from exc
    if root is None:
        raise TallyParseError("response is empty or not XML")
    return root


def raise_for_tally_error(source: bytes | Path) -> None:
    """Raise `TallyResponseError` when the response carries a Tally error.

    Error responses are small, so responses above a size threshold are
    assumed to be data and not scanned.
    """
    if _source_size(source) > _ERROR_SCAN_LIMIT:
        return
    root = parse_root(source)
    tag = _tag_name(root)
    if tag == "RESPONSE":
        text = clean_text(root.text) or ""
        if "running" in text.lower():
            return
        raise TallyResponseError(text or "Tally returned an empty RESPONSE")
    errors = [clean_text(e.text) for e in root.iter("LINEERROR") if clean_text(e.text)]
    if errors:
        raise TallyResponseError("; ".join(str(e) for e in errors))
    if tag != "ENVELOPE":
        raise TallyParseError(f"unexpected root element <{tag}>")
    status = root.findtext("HEADER/STATUS")
    if status is not None and status.strip() == "0":
        raise TallyResponseError("Tally returned STATUS 0 without data")


def iter_elements(source: bytes | Path, tag: str | tuple[str, ...]) -> Iterator[etree._Element]:
    """Stream elements with the given tag(s) from a response.

    Elements are cleared after they are yielded so memory stays bounded.
    Raises `TallyParseError` if the response is not a Tally envelope.
    """
    tags = (tag,) if isinstance(tag, str) else tuple(tag)
    reader = open_sanitized(source)
    saw_envelope = False
    try:
        context = etree.iterparse(
            reader,
            events=("end",),
            tag=tags + ("ENVELOPE",),
            recover=True,
            huge_tree=True,
        )
        for _event, element in context:
            if _tag_name(element) == "ENVELOPE":
                saw_envelope = True
                continue
            if len(element) == 0:  # e.g. <COMPANY>2</COMPANY> counters in CMPINFO
                element.clear()
                continue
            yield element
            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
    except etree.XMLSyntaxError as exc:
        raise TallyParseError(f"response is not valid XML: {exc}") from exc
    finally:
        reader.close()
    if not saw_envelope:
        raise TallyParseError("response is not a Tally ENVELOPE")


def _tag_name(element: etree._Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if not tag.startswith("{"):
        # Plain tag, or a prefixed tag whose namespace Tally never declared
        # (e.g. `UDF:_UDF_788551165.LIST` in collection exports). lxml keeps
        # the latter verbatim and `etree.QName` would reject the colon.
        return tag
    localname = tag.partition("}")[2]
    if element.prefix:
        return f"{element.prefix}:{localname}"
    return localname


def element_to_dict(element: etree._Element) -> Any:
    """Convert an element to plain Python data.

    Leaf elements become their text; containers become dicts keyed by tag.
    Repeated tags and `*.LIST` tags become lists. Container attributes are
    stored under `@NAME`.
    """
    children = [c for c in element if isinstance(c.tag, str)]
    if not children:
        return clean_text(element.text)
    result: dict[str, Any] = {f"@{k}": v for k, v in element.attrib.items()}
    for child in children:
        name = _tag_name(child)
        value = element_to_dict(child)
        if name.endswith(".LIST") or name in result:
            existing = result.get(name)
            if name not in result:
                result[name] = [value]
            elif isinstance(existing, list):
                existing.append(value)
            else:
                result[name] = [existing, value]
        else:
            result[name] = value
    return result


def child_text(element: etree._Element, *tags: str) -> str | None:
    """Text of the first child matching any of `tags`."""
    for tag in tags:
        child = element.find(tag)
        if child is not None:
            text = clean_text(child.text)
            if text is not None:
                return text
    return None


def attr_text(element: etree._Element, name: str) -> str | None:
    return clean_text(element.get(name))


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")


def parse_amount(text: str | None) -> Decimal | None:
    """Parse a Tally amount ("-1,000.00", "1000.00 Dr", "$ 10 @ 82/$ = 820")."""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    if "=" in value:  # multi currency: base amount is after "="
        value = value.rsplit("=", 1)[1].strip()
    lower = value.lower()
    negative = value.startswith("-") or value.startswith("(") or lower.endswith("dr")
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    try:
        number = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    return -number if negative else number


def parse_quantity(text: str | None) -> tuple[Decimal | None, str | None]:
    """Parse a quantity with unit ("10 Nos", "-2.500 Kgs", "1 Box = 10 Nos")."""
    if text is None:
        return None, None
    value = text.strip()
    if not value:
        return None, None
    if "=" in value:
        value = value.split("=", 1)[0].strip()
    match = re.match(r"^(-?)\s*([\d,]*\.?\d+)\s*(.*)$", value)
    if not match:
        return None, clean_text(value)
    sign, number, unit = match.groups()
    try:
        quantity = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None, clean_text(unit)
    return (-quantity if sign else quantity), clean_text(unit)


def parse_rate(text: str | None) -> tuple[Decimal | None, str | None]:
    """Parse a rate with unit ("100.00/Nos")."""
    if text is None:
        return None, None
    value = text.strip()
    if not value:
        return None, None
    unit = None
    if "/" in value:
        value, unit = value.split("/", 1)
    return parse_amount(value), clean_text(unit)


def parse_tally_date(text: str | None) -> date | None:
    """Parse Tally date formats: YYYYMMDD, D-Mon-YY(YY), YYYY-MM-DD, DD/MM/YYYY."""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{8}", value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:]))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})", value)
        if match:
            day, mon, year = match.groups()
            month = _MONTHS.get(mon.lower())
            if month is None:
                return None
            year_int = int(year) + (2000 if len(year) == 2 else 0)
            return date(year_int, month, int(day))
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
        if match:
            day, month, year = (int(g) for g in match.groups())
            return date(year, month, day)
    except ValueError:
        return None
    return None


def parse_bool(text: str | None) -> bool | None:
    if text is None:
        return None
    value = text.strip().lower()
    if value in {"yes", "true", "1"}:
        return True
    if value in {"no", "false", "0"}:
        return False
    return None


def parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    value = text.strip().replace(",", "")
    if not value:
        return None
    try:
        return int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parse reporting
# ---------------------------------------------------------------------------


@dataclass
class ParseReport:
    """Counts of parsed and rejected records for one response."""

    parsed: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)
    rejected_keys: list[tuple[Any, ...]] = field(default_factory=list)
    """Natural keys of rejected records (when identifiable) so they are not treated as deleted."""
    max_errors: int = 20

    def reject(self, message: str, key: tuple[Any, ...] | None = None) -> None:
        self.rejected += 1
        if len(self.errors) < self.max_errors:
            self.errors.append(message)
        if key is not None and all(part not in (None, "") for part in key):
            self.rejected_keys.append(key)


def _language_names(element: etree._Element) -> tuple[str | None, str | None]:
    """Return (name, alias) from LANGUAGENAME.LIST/NAME.LIST."""
    names: list[str] = []
    for lang in element.findall("LANGUAGENAME.LIST"):
        for name_list in lang.findall("NAME.LIST"):
            names.extend(t for n in name_list.findall("NAME") if (t := clean_text(n.text)))
        if names:
            break
    name = names[0] if names else None
    alias = names[1] if len(names) > 1 else None
    return name, alias


def _address_lines(element: etree._Element | None) -> str | None:
    if element is None:
        return None
    lines: list[str] = []
    for address_list in element.findall("ADDRESS.LIST"):
        lines.extend(t for a in address_list.findall("ADDRESS") if (t := clean_text(a.text)))
    if not lines:
        lines.extend(t for a in element.findall("ADDRESS") if (t := clean_text(a.text)))
    return "\n".join(lines) if lines else None


def company_id_hint(name: str | None) -> str | None:
    from stallion_tally.utils.text import slugify

    return slugify(name) if name else None


def _object_name(element: etree._Element) -> str | None:
    return child_text(element, "NAME") or attr_text(element, "NAME")


# ---------------------------------------------------------------------------
# Dataset parsers
# ---------------------------------------------------------------------------


def parse_companies(source: bytes | Path, report: ParseReport | None = None) -> list[Company]:
    report = report or ParseReport()
    companies: list[Company] = []
    for element in iter_elements(source, "COMPANY"):
        name = _object_name(element)
        try:
            company = Company(
                name=name or "",
                tally_guid=child_text(element, "GUID"),
                formal_name=child_text(element, "BASICCOMPANYFORMALNAME"),
                mailing_name=child_text(element, "MAILINGNAME"),
                company_number=child_text(element, "COMPANYNUMBER"),
                starting_from=parse_tally_date(child_text(element, "STARTINGFROM")),
                books_from=parse_tally_date(child_text(element, "BOOKSFROM")),
                ending_at=parse_tally_date(child_text(element, "ENDINGAT")),
                base_currency_symbol=child_text(element, "BASECURRENCYSYMBOL"),
                base_currency_name=child_text(element, "BASECURRENCYNAME"),
                alter_id=parse_int(child_text(element, "ALTERID")),
                master_id=parse_int(child_text(element, "MASTERID")),
                email=child_text(element, "EMAIL"),
                state=child_text(element, "STATENAME"),
                country=child_text(element, "COUNTRYNAME"),
                pincode=child_text(element, "PINCODE"),
                address=_address_lines(element),
                raw_data=element_to_dict(element),
            )
        except (ValidationError, ValueError) as exc:
            report.reject(f"company {name!r}: {exc}", key=(company_id_hint(name),))
            continue
        companies.append(company)
        report.parsed += 1
    return companies


def parse_groups(
    source: bytes | Path, company: Company, report: ParseReport | None = None
) -> Iterator[Group]:
    report = report or ParseReport()
    for element in iter_elements(source, "GROUP"):
        name = _object_name(element)
        lang_name, alias = _language_names(element)
        try:
            yield Group(
                company_id=company.company_id,
                company_name=company.name,
                name=name or lang_name or "",
                tally_guid=child_text(element, "GUID"),
                parent=child_text(element, "PARENT"),
                alias=alias,
                alter_id=parse_int(child_text(element, "ALTERID")),
                master_id=parse_int(child_text(element, "MASTERID")),
                is_revenue=parse_bool(child_text(element, "ISREVENUE")),
                is_deemed_positive=parse_bool(child_text(element, "ISDEEMEDPOSITIVE")),
                affects_gross_profit=parse_bool(child_text(element, "AFFECTSGROSSPROFIT")),
                is_subledger=parse_bool(child_text(element, "ISSUBLEDGER")),
                is_addable=parse_bool(child_text(element, "ISADDABLE")),
                sort_position=parse_int(child_text(element, "SORTPOSITION")),
                raw_data=element_to_dict(element),
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(f"group {name!r}: {exc}", key=(company.company_id, name or lang_name))


def _latest(element: etree._Element, list_tag: str) -> etree._Element | None:
    items = element.findall(list_tag)
    return items[-1] if items else None


def parse_ledgers(
    source: bytes | Path, company: Company, report: ParseReport | None = None
) -> Iterator[Ledger]:
    report = report or ParseReport()
    for element in iter_elements(source, "LEDGER"):
        name = _object_name(element)
        lang_name, alias = _language_names(element)
        mailing = _latest(element, "LEDMAILINGDETAILS.LIST")
        gst = _latest(element, "LEDGSTREGDETAILS.LIST")
        try:
            yield Ledger(
                company_id=company.company_id,
                company_name=company.name,
                name=name or lang_name or "",
                tally_guid=child_text(element, "GUID"),
                parent_group=child_text(element, "PARENT"),
                alias=alias,
                alter_id=parse_int(child_text(element, "ALTERID")),
                master_id=parse_int(child_text(element, "MASTERID")),
                opening_balance=parse_amount(child_text(element, "OPENINGBALANCE")),
                currency=child_text(element, "CURRENCYNAME"),
                is_billwise_on=parse_bool(child_text(element, "ISBILLWISEON")),
                is_cost_centres_on=parse_bool(child_text(element, "ISCOSTCENTRESON")),
                is_revenue=parse_bool(child_text(element, "ISREVENUE")),
                is_deemed_positive=parse_bool(child_text(element, "ISDEEMEDPOSITIVE")),
                mailing_name=(
                    (child_text(mailing, "MAILINGNAME") if mailing is not None else None)
                    or _first_of_list(element, "MAILINGNAME.LIST", "MAILINGNAME")
                ),
                address=_address_lines(mailing) or _address_lines(element),
                state=(child_text(mailing, "STATE") if mailing is not None else None)
                or child_text(element, "LEDSTATENAME"),
                country=(child_text(mailing, "COUNTRY") if mailing is not None else None)
                or child_text(element, "COUNTRYNAME"),
                pincode=(child_text(mailing, "PINCODE") if mailing is not None else None)
                or child_text(element, "PINCODE"),
                gstin=(child_text(gst, "GSTIN") if gst is not None else None)
                or child_text(element, "PARTYGSTIN"),
                gst_registration_type=(
                    child_text(gst, "GSTREGISTRATIONTYPE") if gst is not None else None
                )
                or child_text(element, "GSTREGISTRATIONTYPE"),
                pan=child_text(element, "INCOMETAXNUMBER"),
                phone=child_text(element, "LEDGERPHONE"),
                mobile=child_text(element, "LEDGERMOBILE"),
                email=child_text(element, "EMAIL"),
                credit_limit=parse_amount(child_text(element, "CREDITLIMIT")),
                credit_period=child_text(element, "BILLCREDITPERIOD"),
                raw_data=element_to_dict(element),
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(f"ledger {name!r}: {exc}", key=(company.company_id, name or lang_name))


def _first_of_list(element: etree._Element, list_tag: str, item_tag: str) -> str | None:
    for lst in element.findall(list_tag):
        for item in lst.findall(item_tag):
            text = clean_text(item.text)
            if text:
                return text
    return None


def _hsn_code(element: etree._Element) -> str | None:
    for tag in ("GSTDETAILS.LIST", "HSNDETAILS.LIST"):
        for details in element.findall(tag):
            code = child_text(details, "HSNCODE")
            if code:
                return code
            for inner in details.findall("HSNDETAILS.LIST"):
                code = child_text(inner, "HSNCODE")
                if code:
                    return code
    return child_text(element, "HSNCODE")


def parse_stock_items(
    source: bytes | Path, company: Company, report: ParseReport | None = None
) -> Iterator[StockItem]:
    report = report or ParseReport()
    for element in iter_elements(source, "STOCKITEM"):
        name = _object_name(element)
        lang_name, alias = _language_names(element)
        quantity, unit = parse_quantity(child_text(element, "OPENINGBALANCE"))
        rate, _ = parse_rate(child_text(element, "OPENINGRATE"))
        try:
            yield StockItem(
                company_id=company.company_id,
                company_name=company.name,
                name=name or lang_name or "",
                tally_guid=child_text(element, "GUID"),
                parent=child_text(element, "PARENT"),
                category=child_text(element, "CATEGORY"),
                alias=alias,
                base_unit=child_text(element, "BASEUNITS") or unit,
                additional_unit=child_text(element, "ADDITIONALUNITS"),
                alter_id=parse_int(child_text(element, "ALTERID")),
                master_id=parse_int(child_text(element, "MASTERID")),
                opening_quantity=quantity,
                opening_value=parse_amount(child_text(element, "OPENINGVALUE")),
                opening_rate=rate,
                hsn_code=_hsn_code(element),
                part_number=child_text(element, "PARTNO"),
                description=child_text(element, "DESCRIPTION"),
                gst_applicable=child_text(element, "GSTAPPLICABLE"),
                raw_data=element_to_dict(element),
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(
                f"stock item {name!r}: {exc}", key=(company.company_id, name or lang_name)
            )


_LEDGER_ENTRY_TAGS = ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST")
_INVENTORY_ENTRY_TAGS = ("ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST")
_LEDGER_ENTRY_FIELDS = {
    "LEDGERNAME", "AMOUNT", "ISDEEMEDPOSITIVE", "ISPARTYLEDGER", "BILLALLOCATIONS.LIST",
}  # fmt: skip
_INVENTORY_ENTRY_FIELDS = {
    "STOCKITEMNAME", "ISDEEMEDPOSITIVE", "RATE", "AMOUNT", "ACTUALQTY", "BILLEDQTY", "DISCOUNT",
}  # fmt: skip


def _bill_allocations(entry: etree._Element) -> list[BillAllocation]:
    allocations: list[BillAllocation] = []
    for alloc in entry.findall("BILLALLOCATIONS.LIST"):
        allocations.append(
            BillAllocation(
                name=child_text(alloc, "NAME"),
                bill_type=child_text(alloc, "BILLTYPE"),
                amount=parse_amount(child_text(alloc, "AMOUNT")),
                credit_period=child_text(alloc, "BILLCREDITPERIOD"),
            )
        )
    return allocations


def _extra_fields(element: etree._Element, consumed: set[str]) -> dict[str, Any] | None:
    data = element_to_dict(element)
    if not isinstance(data, dict):
        return None
    extra = {k: v for k, v in data.items() if k not in consumed and not k.startswith("@")}
    return extra or None


def _ledger_entries(voucher: etree._Element) -> list[VoucherLedgerEntry]:
    entries: list[VoucherLedgerEntry] = []
    line_no = 0
    for tag in _LEDGER_ENTRY_TAGS:
        for entry in voucher.findall(tag):
            ledger_name = child_text(entry, "LEDGERNAME")
            if not ledger_name:
                continue
            line_no += 1
            amount = parse_amount(child_text(entry, "AMOUNT"))
            debit = credit = None
            if amount is not None:
                if amount < 0:
                    debit = -amount
                else:
                    credit = amount
            entries.append(
                VoucherLedgerEntry(
                    line_no=line_no,
                    ledger_name=ledger_name,
                    amount=amount,
                    debit=debit,
                    credit=credit,
                    is_deemed_positive=parse_bool(child_text(entry, "ISDEEMEDPOSITIVE")),
                    is_party_ledger=parse_bool(child_text(entry, "ISPARTYLEDGER")),
                    bill_allocations=_bill_allocations(entry),
                    extra=_extra_fields(entry, _LEDGER_ENTRY_FIELDS),
                )
            )
        if entries:
            break
    return entries


def _inventory_entries(voucher: etree._Element) -> list[VoucherInventoryEntry]:
    entries: list[VoucherInventoryEntry] = []
    line_no = 0
    for tag in _INVENTORY_ENTRY_TAGS:
        for entry in voucher.findall(tag):
            item_name = child_text(entry, "STOCKITEMNAME")
            if not item_name:
                continue
            line_no += 1
            actual_qty, unit = parse_quantity(child_text(entry, "ACTUALQTY"))
            billed_qty, billed_unit = parse_quantity(child_text(entry, "BILLEDQTY"))
            rate, rate_unit = parse_rate(child_text(entry, "RATE"))
            batch = entry.find("BATCHALLOCATIONS.LIST")
            accounting = entry.find("ACCOUNTINGALLOCATIONS.LIST")
            entries.append(
                VoucherInventoryEntry(
                    line_no=line_no,
                    stock_item_name=item_name,
                    quantity=billed_qty if billed_qty is not None else actual_qty,
                    unit=unit or billed_unit or rate_unit,
                    actual_quantity=actual_qty,
                    billed_quantity=billed_qty,
                    rate=rate,
                    rate_unit=rate_unit,
                    amount=parse_amount(child_text(entry, "AMOUNT")),
                    discount=parse_amount(child_text(entry, "DISCOUNT")),
                    godown_name=child_text(batch, "GODOWNNAME") if batch is not None else None,
                    batch_name=child_text(batch, "BATCHNAME") if batch is not None else None,
                    accounting_ledger=(
                        child_text(accounting, "LEDGERNAME") if accounting is not None else None
                    ),
                    is_deemed_positive=parse_bool(child_text(entry, "ISDEEMEDPOSITIVE")),
                    extra=_extra_fields(entry, _INVENTORY_ENTRY_FIELDS),
                )
            )
        if entries:
            break
    return entries


def parse_vouchers(
    source: bytes | Path, company: Company, report: ParseReport | None = None
) -> Iterator[Voucher]:
    """Stream vouchers from a Day Book export."""
    report = report or ParseReport()
    for element in iter_elements(source, "VOUCHER"):
        guid = child_text(element, "GUID") or attr_text(element, "REMOTEID")
        number = child_text(element, "VOUCHERNUMBER")
        try:
            ledger_entries = _ledger_entries(element)
            debits = [e.debit for e in ledger_entries if e.debit is not None]
            credits = [e.credit for e in ledger_entries if e.credit is not None]
            yield Voucher(
                company_id=company.company_id,
                company_name=company.name,
                tally_guid=guid or "",
                voucher_type=child_text(element, "VOUCHERTYPENAME")
                or attr_text(element, "VCHTYPE")
                or "",
                voucher_number=number,
                date=parse_tally_date(child_text(element, "DATE")),  # type: ignore[arg-type]
                effective_date=parse_tally_date(child_text(element, "EFFECTIVEDATE")),
                reference=child_text(element, "REFERENCE"),
                reference_date=parse_tally_date(child_text(element, "REFERENCEDATE")),
                party_ledger_name=child_text(element, "PARTYLEDGERNAME"),
                narration=child_text(element, "NARRATION"),
                alter_id=parse_int(child_text(element, "ALTERID")),
                master_id=parse_int(child_text(element, "MASTERID")),
                remote_id=attr_text(element, "REMOTEID"),
                vch_key=attr_text(element, "VCHKEY"),
                voucher_key=child_text(element, "VOUCHERKEY"),
                persisted_view=child_text(element, "PERSISTEDVIEW")
                or attr_text(element, "OBJVIEW"),
                is_cancelled=parse_bool(child_text(element, "ISCANCELLED")) or False,
                is_optional=parse_bool(child_text(element, "ISOPTIONAL")) or False,
                is_invoice=parse_bool(child_text(element, "ISINVOICE")),
                is_post_dated=parse_bool(child_text(element, "ISPOSTDATED")),
                total_debit=sum(debits, Decimal(0)) if debits else None,
                total_credit=sum(credits, Decimal(0)) if credits else None,
                ledger_entries=ledger_entries,
                inventory_entries=_inventory_entries(element),
                raw_data=element_to_dict(element),
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(
                f"voucher guid={guid!r} number={number!r}: {exc}", key=(company.company_id, guid)
            )


def _bill_type_from_amount(amount: Decimal | None) -> str:
    if amount is None:
        return "unknown"
    # Tally sign convention: negative = debit. A debit closing balance on a
    # party ledger is money owed to us (receivable).
    return "receivable" if amount < 0 else "payable"


def parse_bills_collection(
    source: bytes | Path, company: Company, report: ParseReport | None = None
) -> list[Bill]:
    """Parse bills returned by the `Bills` TDL collection."""
    report = report or ParseReport()
    root = parse_root(source)
    container = root.find(".//COLLECTION")
    candidates = list(container) if container is not None else list(root.iter("BILL", "BILLS"))
    bills: list[Bill] = []
    for element in candidates:
        if not isinstance(element.tag, str) or len(element) == 0:
            continue
        ledger = child_text(element, "PARENT", "LEDGERNAME", "BILLPARTY")
        ref = _object_name(element) or child_text(element, "BILLREF")
        closing = parse_amount(child_text(element, "CLOSINGBALANCE", "BILLCL"))
        if not ledger or not ref:
            report.reject(f"bill ref={ref!r}: missing ledger or reference")
            continue
        try:
            bills.append(
                Bill(
                    company_id=company.company_id,
                    company_name=company.name,
                    ledger_name=ledger,
                    bill_ref=ref,
                    bill_type=_bill_type_from_amount(closing),
                    bill_date=parse_tally_date(child_text(element, "BILLDATE")),
                    due_date=parse_tally_date(child_text(element, "FINALDUEDATE", "BILLDUE")),
                    credit_period=child_text(element, "BILLCREDITPERIOD"),
                    opening_amount=parse_amount(child_text(element, "OPENINGBALANCE")),
                    closing_amount=closing,
                    overdue_days=parse_int(child_text(element, "OVERDUEDAYS", "BILLOVERDUE")),
                    is_advance=parse_bool(child_text(element, "ISADVANCE")),
                    raw_data=element_to_dict(element),
                )
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(f"bill ref={ref!r}: {exc}", key=(company.company_id, ledger, ref))
    return bills


def parse_bills_report(
    source: bytes | Path,
    company: Company,
    bill_type: str,
    report: ParseReport | None = None,
) -> list[Bill]:
    """Parse the Bills Receivable / Bills Payable report export.

    The report is a flat sequence: each BILLFIXED element starts a bill and
    the BILLCL / BILLDUE / BILLOVERDUE siblings that follow belong to it.
    """
    report = report or ParseReport()
    root = parse_root(source)
    bills: list[Bill] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        ledger = current.get("BILLPARTY")
        ref = current.get("BILLREF")
        if not ledger or not ref:
            report.reject(f"bill ref={ref!r}: missing party or reference")
            current = None
            return
        try:
            bills.append(
                Bill(
                    company_id=company.company_id,
                    company_name=company.name,
                    ledger_name=ledger,
                    bill_ref=ref,
                    bill_type=bill_type,  # type: ignore[arg-type]
                    bill_date=parse_tally_date(current.get("BILLDATE")),
                    due_date=parse_tally_date(current.get("BILLDUE")),
                    closing_amount=parse_amount(current.get("BILLCL")),
                    overdue_days=parse_int(current.get("BILLOVERDUE")),
                    raw_data=dict(current),
                )
            )
            report.parsed += 1
        except (ValidationError, ValueError) as exc:
            report.reject(f"bill ref={ref!r}: {exc}", key=(company.company_id, ledger, ref))
        current = None

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = _tag_name(element)
        if tag == "BILLFIXED":
            flush()
            current = {}
            for child in element:
                if isinstance(child.tag, str):
                    current[_tag_name(child)] = clean_text(child.text)
        elif tag in {"BILLCL", "BILLDUE", "BILLOVERDUE"} and current is not None:
            current[tag] = clean_text(element.text)
    flush()
    return bills


@dataclass(frozen=True)
class ProbedVoucher:
    """Identity and flags of a voucher, from a lightweight probe request."""

    guid: str | None
    voucher_type: str | None
    voucher_number: str | None
    date: date | None
    master_id: int | None
    is_optional: bool
    is_cancelled: bool
    is_post_dated: bool
    is_deleted: bool
    persisted_view: str | None


def parse_voucher_probe(source: bytes | Path) -> list[ProbedVoucher]:
    """Parse a `build_voucher_probe_request` response."""
    probed: list[ProbedVoucher] = []
    for element in iter_elements(source, "VOUCHER"):
        probed.append(
            ProbedVoucher(
                guid=child_text(element, "GUID") or attr_text(element, "REMOTEID"),
                voucher_type=child_text(element, "VOUCHERTYPENAME")
                or attr_text(element, "VCHTYPE"),
                voucher_number=child_text(element, "VOUCHERNUMBER"),
                date=parse_tally_date(child_text(element, "DATE")),
                master_id=parse_int(child_text(element, "MASTERID")),
                is_optional=parse_bool(child_text(element, "ISOPTIONAL")) or False,
                is_cancelled=parse_bool(child_text(element, "ISCANCELLED")) or False,
                is_post_dated=parse_bool(child_text(element, "ISPOSTDATED")) or False,
                is_deleted=parse_bool(child_text(element, "ISDELETED")) or False,
                persisted_view=child_text(element, "PERSISTEDVIEW")
                or attr_text(element, "OBJVIEW"),
            )
        )
    return probed
