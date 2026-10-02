"""Deterministic consistency checks on extracted data, for a Tally expert to review.

Sign convention (as stored by the normalizer): ledger entry `debit`/`credit` are
positive amounts; ledger opening balances and bill amounts keep Tally's sign,
where negative means Debit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from stallion_tally.reports.loader import CompanyReportData

Status = Literal["PASS", "FAIL", "WARN", "INFO"]

TOLERANCE = Decimal("0.01")
ZERO = Decimal(0)
# Tally's reserved top level; primary groups report it (or nothing) as their parent.
ROOT_PARENTS = frozenset({"", "primary", "\x04 primary"})


@dataclass
class Issue:
    reference: str
    detail: str
    date: object = None
    voucher_type: str | None = None
    voucher_number: str | None = None
    guid: str | None = None


@dataclass
class CheckResult:
    code: str
    title: str
    status: Status
    summary: str
    meaning: str
    issues: list[Issue] = field(default_factory=list)


@dataclass
class LedgerMovement:
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass
class VoucherTypeSummary:
    voucher_type: str
    count: int = 0
    cancelled: int = 0
    optional: int = 0
    debit: Decimal = ZERO


def split_dr_cr(amount: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """Tally signed amount -> (debit, credit) as positive numbers (negative = Debit)."""
    if amount is None or amount == 0:
        return None, None
    return (-amount, None) if amount < 0 else (None, amount)


INVOICE_VIEW = "Invoice Voucher View"


def is_non_accounting(voucher: object) -> bool:
    """Orders, delivery notes and receipt notes: invoice-view vouchers that are not invoices.

    Their ledger lines are informational; Tally does not post them to the books.
    """
    return (
        voucher.persisted_view == INVOICE_VIEW  # type: ignore[attr-defined]
        and voucher.is_invoice is False  # type: ignore[attr-defined]
    )


def counts_in_books(voucher: object) -> bool:
    """Whether the voucher's amounts reach the ledgers.

    Optional vouchers do not post, cancelled ones carry no amounts and
    non-accounting vouchers (orders, delivery/receipt notes) only move or
    promise stock.
    """
    return not (
        voucher.is_cancelled  # type: ignore[attr-defined]
        or voucher.is_optional  # type: ignore[attr-defined]
        or is_non_accounting(voucher)
    )


def ledger_list_is_complete(voucher: object) -> bool:
    """True when Tally sent ALLLEDGERENTRIES, which already holds the sales/purchase ledger."""
    raw = voucher.raw_json  # type: ignore[attr-defined]
    return isinstance(raw, dict) and "ALLLEDGERENTRIES.LIST" in raw


@dataclass(frozen=True)
class Posting:
    """One Debit or Credit to a ledger made by a voucher."""

    voucher_guid: str
    line_no: int
    ledger_name: str
    debit: Decimal | None
    credit: Decimal | None
    source: str  # "Ledger entry" | "Item invoice"
    stock_item: str | None = None
    bill_allocations: object = None
    is_party_ledger: bool | None = None


def postings_by_voucher(data: CompanyReportData) -> dict[str, list[Posting]]:
    """Ledger postings per voucher, including item-invoice ledger postings when needed.

    Tally's ALLLEDGERENTRIES list (the voucher collection request) already
    contains the Sales/Purchase ledger of an item invoice. Only when a response
    carries just LEDGERENTRIES (the older Day Book export) is that ledger
    missing; it then lives in each stock line's accounting allocation and is
    added from there. Non-invoice vouchers (orders, delivery notes, stock
    journals) never take amounts from stock lines.
    """
    vouchers = {v.tally_guid: v for v in data.vouchers}
    result: dict[str, list[Posting]] = defaultdict(list)
    for e in data.ledger_entries:
        result[e.voucher_guid].append(
            Posting(
                e.voucher_guid,
                e.line_no,
                e.ledger_name,
                e.debit or None,
                e.credit or None,
                "Ledger entry",
                bill_allocations=e.bill_allocations_json,
                is_party_ledger=e.is_party_ledger,
            )
        )
    for e in data.inventory_entries:
        voucher = vouchers.get(e.voucher_guid)
        if (
            not e.accounting_ledger
            or voucher is None
            or voucher.is_invoice is not True
            or ledger_list_is_complete(voucher)
        ):
            continue
        debit, credit = split_dr_cr(e.amount)
        result[e.voucher_guid].append(
            Posting(
                e.voucher_guid,
                e.line_no,
                e.accounting_ledger,
                debit,
                credit,
                "Item invoice",
                stock_item=e.stock_item_name,
            )
        )
    return dict(result)


def voucher_totals(data: CompanyReportData) -> dict[str, tuple[Decimal, Decimal]]:
    """(total Debit, total Credit) per voucher GUID from all its postings."""
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for guid, postings in postings_by_voucher(data).items():
        totals[guid] = (
            sum((p.debit or ZERO for p in postings), ZERO),
            sum((p.credit or ZERO for p in postings), ZERO),
        )
    return totals


def ledger_movements(data: CompanyReportData) -> dict[str, LedgerMovement]:
    posting_vouchers = {v.tally_guid for v in data.vouchers if counts_in_books(v)}
    result: dict[str, LedgerMovement] = defaultdict(LedgerMovement)
    for guid, postings in postings_by_voucher(data).items():
        if guid not in posting_vouchers:
            continue
        for p in postings:
            movement = result[p.ledger_name]
            movement.debit += p.debit or ZERO
            movement.credit += p.credit or ZERO
    return dict(result)


def voucher_type_summaries(data: CompanyReportData) -> list[VoucherTypeSummary]:
    totals = voucher_totals(data)
    by_type: dict[str, VoucherTypeSummary] = {}
    for voucher in data.vouchers:
        summary = by_type.setdefault(voucher.voucher_type, VoucherTypeSummary(voucher.voucher_type))
        summary.count += 1
        summary.cancelled += int(voucher.is_cancelled)
        summary.optional += int(voucher.is_optional)
        if counts_in_books(voucher):
            summary.debit += totals.get(voucher.tally_guid, (ZERO, ZERO))[0]
    return sorted(by_type.values(), key=lambda s: s.voucher_type.lower())


def _vch_issue(voucher: object, reference: str, detail: str) -> Issue:
    return Issue(
        reference=reference,
        detail=detail,
        date=voucher.date,  # type: ignore[attr-defined]
        voucher_type=voucher.voucher_type,  # type: ignore[attr-defined]
        voucher_number=voucher.voucher_number,  # type: ignore[attr-defined]
        guid=voucher.tally_guid,  # type: ignore[attr-defined]
    )


def _status(issues: list[Issue], failure: Status = "FAIL") -> Status:
    return failure if issues else "PASS"


def check_vouchers_balanced(data: CompanyReportData) -> CheckResult:
    totals = voucher_totals(data)
    issues = []
    for v in data.vouchers:
        if not counts_in_books(v):
            continue
        dr, cr = totals.get(v.tally_guid, (ZERO, ZERO))
        if abs(dr - cr) > TOLERANCE:
            issues.append(
                _vch_issue(v, v.party_ledger_name or "", f"Debit {dr:,.2f} <> Credit {cr:,.2f}")
            )
    return CheckResult(
        "C01",
        "Every voucher balances (Debit = Credit)",
        _status(issues),
        f"{len(issues)} unbalanced voucher(s)" if issues else "All vouchers balance",
        "Tally never saves an unbalanced voucher, so a failure means lines were lost or "
        "misread during extraction. Open the voucher in Tally and compare its lines with "
        "the 'Voucher Lines' sheet.",
        issues,
    )


def period_totals(data: CompanyReportData) -> tuple[Decimal, Decimal]:
    """Total Debit and Credit of all vouchers that post to the books."""
    totals = voucher_totals(data)
    dr = cr = ZERO
    for v in data.vouchers:
        if counts_in_books(v):
            d, c = totals.get(v.tally_guid, (ZERO, ZERO))
            dr += d
            cr += c
    return dr, cr


def check_period_totals(data: CompanyReportData) -> CheckResult:
    dr, cr = period_totals(data)
    ok = abs(dr - cr) <= TOLERANCE
    return CheckResult(
        "C02",
        "Day Book total Debit = total Credit",
        "PASS" if ok else "FAIL",
        f"Debit {dr:,.2f} / Credit {cr:,.2f}",
        "Totals for all posting vouchers in the period. Compare with the Day Book totals "
        "in Tally for the same period.",
    )


def check_opening_balances(data: CompanyReportData) -> CheckResult:
    dr = cr = ZERO
    for ledger in data.ledgers:
        d, c = split_dr_cr(ledger.opening_balance)
        dr += d or ZERO
        cr += c or ZERO
    diff = dr - cr
    ok = abs(diff) <= TOLERANCE
    return CheckResult(
        "C03",
        "Ledger opening balances agree (Debit = Credit)",
        "PASS" if ok else "WARN",
        f"Debit {dr:,.2f} / Credit {cr:,.2f}" + ("" if ok else f" / Difference {diff:,.2f}"),
        "A difference is normal only if Tally itself shows 'Difference in opening balances' "
        "(or opening stock) in the Trial Balance. Otherwise ledger openings were misread.",
    )


def check_unknown_ledgers(data: CompanyReportData) -> CheckResult:
    known = {ledger.name.lower() for ledger in data.ledgers}
    vouchers = {v.tally_guid: v for v in data.vouchers}
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for guid, postings in postings_by_voucher(data).items():
        for p in postings:
            key = (guid, p.ledger_name)
            if p.ledger_name.lower() in known or key in seen:
                continue
            seen.add(key)
            voucher = vouchers.get(guid)
            detail = "Ledger used in a voucher but missing from the ledger master list"
            issues.append(
                _vch_issue(voucher, p.ledger_name, detail)
                if voucher
                else Issue(p.ledger_name, detail, guid=guid)
            )
    for bill in data.bills:
        if bill.ledger_name.lower() not in known:
            issues.append(
                Issue(bill.ledger_name, f"Bill {bill.bill_ref}: party ledger missing from masters")
            )
    return CheckResult(
        "C04",
        "All ledgers used in vouchers and bills exist in the ledger master",
        _status(issues),
        f"{len(issues)} reference(s) to unknown ledgers" if issues else "All ledgers found",
        "Usually means the ledger list was extracted before the ledger was created, or a "
        "ledger was renamed. Re-run sync and check again.",
        issues,
    )


def check_unknown_groups(data: CompanyReportData) -> CheckResult:
    known = {g.name.lower() for g in data.groups}
    issues: list[Issue] = []
    if data.groups:
        for group in data.groups:
            parent = (group.parent or "").strip()
            if parent.lower() not in ROOT_PARENTS and parent.lower() not in known:
                issues.append(Issue(group.name, f"Group is under unknown group '{parent}'"))
        for ledger in data.ledgers:
            parent = (ledger.parent_group or "").strip()
            if not parent:
                issues.append(Issue(ledger.name, "Ledger has no parent group"))
            elif parent.lower() not in known:
                issues.append(Issue(ledger.name, f"Ledger is under unknown group '{parent}'"))
    return CheckResult(
        "C05",
        "Every ledger and group sits under a known group",
        _status(issues, "WARN") if data.groups else "INFO",
        f"{len(issues)} item(s) under unknown groups"
        if issues
        else ("Hierarchy complete" if data.groups else "No groups extracted"),
        "Compare the 'Under' column with the Chart of Accounts in Tally.",
        issues,
    )


def check_unknown_stock_items(data: CompanyReportData) -> CheckResult:
    known = {s.name.lower() for s in data.stock_items}
    vouchers = {v.tally_guid: v for v in data.vouchers}
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for entry in data.inventory_entries:
        key = (entry.voucher_guid, entry.stock_item_name)
        if entry.stock_item_name.lower() in known or key in seen:
            continue
        seen.add(key)
        voucher = vouchers.get(entry.voucher_guid)
        detail = "Stock item used in a voucher but missing from the stock item list"
        issues.append(
            _vch_issue(voucher, entry.stock_item_name, detail)
            if voucher
            else Issue(entry.stock_item_name, detail, guid=entry.voucher_guid)
        )
    return CheckResult(
        "C06",
        "All stock items used in vouchers exist in the stock item master",
        _status(issues),
        f"{len(issues)} reference(s) to unknown items" if issues else "All stock items found",
        "Same cause as C04, for inventory.",
        issues,
    )


def check_empty_vouchers(data: CompanyReportData) -> CheckResult:
    issues = [
        _vch_issue(v, v.party_ledger_name or "", "Voucher has no ledger or inventory lines")
        for v in data.vouchers
        if counts_in_books(v) and v.ledger_entry_count == 0 and v.inventory_entry_count == 0
    ]
    return CheckResult(
        "C07",
        "No posting voucher is empty",
        _status(issues),
        f"{len(issues)} empty voucher(s)" if issues else "All vouchers have lines",
        "An empty voucher usually means Tally returned the voucher header without its "
        "entries. Open it in Tally to confirm.",
        issues,
    )


def check_duplicate_numbers(data: CompanyReportData) -> CheckResult:
    counter = Counter(
        (v.voucher_type, v.voucher_number)
        for v in data.vouchers
        if v.voucher_number and not v.is_cancelled
    )
    issues = [
        _vch_issue(v, v.party_ledger_name or "", f"Voucher number used {counter[key]} times")
        for v in data.vouchers
        if (key := (v.voucher_type, v.voucher_number)) in counter
        and counter[key] > 1
        and not v.is_cancelled
    ]
    return CheckResult(
        "C08",
        "Voucher numbers are unique within each voucher type",
        _status(issues, "WARN"),
        f"{len(issues)} voucher(s) share a number" if issues else "No duplicates",
        "Tally allows duplicates with manual numbering, so this is a warning. Confirm they "
        "are genuinely separate vouchers in Tally (not extracted twice).",
        issues,
    )


def check_data_present(data: CompanyReportData) -> CheckResult:
    missing = [
        name
        for name, rows in (
            ("groups", data.groups),
            ("ledgers", data.ledgers),
            ("vouchers in period", data.vouchers),
        )
        if not rows
    ]
    return CheckResult(
        "C09",
        "Core datasets were extracted",
        "WARN" if missing else "PASS",
        ("Nothing extracted for: " + ", ".join(missing))
        if missing
        else "Groups, ledgers and vouchers present",
        "Empty data can be correct (e.g. no vouchers in the period) but should be confirmed "
        "in Tally.",
    )


ALL_CHECKS = (
    check_data_present,
    check_vouchers_balanced,
    check_period_totals,
    check_opening_balances,
    check_unknown_ledgers,
    check_unknown_groups,
    check_unknown_stock_items,
    check_empty_vouchers,
    check_duplicate_numbers,
)


def run_checks(data: CompanyReportData) -> list[CheckResult]:
    return sorted((check(data) for check in ALL_CHECKS), key=lambda c: c.code)
