"""Validated models -> database row dictionaries (with record hashes)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from stallion_tally.models import Bill, Company, Group, Ledger, StockItem, Voucher
from stallion_tally.utils.hashing import hash_record

_QUANT = Decimal("0.000001")
_VOLATILE = frozenset({"snapshot_at", "record_hash"})


def money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value).quantize(_QUANT, rounding=ROUND_HALF_UP)


def _finish(row: dict[str, Any]) -> dict[str, Any]:
    hashable = {k: v for k, v in row.items() if k not in _VOLATILE}
    row["record_hash"] = hash_record(hashable)
    return row


def normalize_company(company: Company) -> dict[str, Any]:
    return _finish(
        {
            "company_id": company.company_id,
            "company_name": company.name,
            "tally_guid": company.tally_guid,
            "formal_name": company.formal_name,
            "mailing_name": company.mailing_name,
            "company_number": company.company_number,
            "starting_from": company.starting_from,
            "books_from": company.books_from,
            "ending_at": company.ending_at,
            "base_currency_symbol": company.base_currency_symbol,
            "base_currency_name": company.base_currency_name,
            "alter_id": company.alter_id,
            "master_id": company.master_id,
            "email": company.email,
            "state": company.state,
            "country": company.country,
            "pincode": company.pincode,
            "address": company.address,
            "raw_json": company.raw_data,
            "is_deleted": False,
        }
    )


def normalize_group(group: Group) -> dict[str, Any]:
    return _finish(
        {
            "company_id": group.company_id,
            "company_name": group.company_name,
            "name": group.name,
            "tally_guid": group.tally_guid,
            "parent": group.parent,
            "alias": group.alias,
            "alter_id": group.alter_id,
            "master_id": group.master_id,
            "is_revenue": group.is_revenue,
            "is_deemed_positive": group.is_deemed_positive,
            "affects_gross_profit": group.affects_gross_profit,
            "is_subledger": group.is_subledger,
            "is_addable": group.is_addable,
            "sort_position": group.sort_position,
            "raw_json": group.raw_data,
            "is_deleted": group.is_deleted,
        }
    )


def normalize_ledger(ledger: Ledger) -> dict[str, Any]:
    return _finish(
        {
            "company_id": ledger.company_id,
            "company_name": ledger.company_name,
            "name": ledger.name,
            "tally_guid": ledger.tally_guid,
            "parent_group": ledger.parent_group,
            "alias": ledger.alias,
            "alter_id": ledger.alter_id,
            "master_id": ledger.master_id,
            "opening_balance": money(ledger.opening_balance),
            "currency": ledger.currency,
            "is_billwise_on": ledger.is_billwise_on,
            "is_cost_centres_on": ledger.is_cost_centres_on,
            "is_revenue": ledger.is_revenue,
            "is_deemed_positive": ledger.is_deemed_positive,
            "mailing_name": ledger.mailing_name,
            "address": ledger.address,
            "state": ledger.state,
            "country": ledger.country,
            "pincode": ledger.pincode,
            "gstin": ledger.gstin,
            "gst_registration_type": ledger.gst_registration_type,
            "pan": ledger.pan,
            "phone": ledger.phone,
            "mobile": ledger.mobile,
            "email": ledger.email,
            "credit_limit": money(ledger.credit_limit),
            "credit_period": ledger.credit_period,
            "raw_json": ledger.raw_data,
            "is_deleted": ledger.is_deleted,
        }
    )


def normalize_stock_item(item: StockItem) -> dict[str, Any]:
    return _finish(
        {
            "company_id": item.company_id,
            "company_name": item.company_name,
            "name": item.name,
            "tally_guid": item.tally_guid,
            "parent": item.parent,
            "category": item.category,
            "alias": item.alias,
            "base_unit": item.base_unit,
            "additional_unit": item.additional_unit,
            "alter_id": item.alter_id,
            "master_id": item.master_id,
            "opening_quantity": money(item.opening_quantity),
            "opening_value": money(item.opening_value),
            "opening_rate": money(item.opening_rate),
            "hsn_code": item.hsn_code,
            "part_number": item.part_number,
            "description": item.description,
            "gst_applicable": item.gst_applicable,
            "raw_json": item.raw_data,
            "is_deleted": item.is_deleted,
        }
    )


def normalize_bill(bill: Bill, snapshot_at: datetime) -> dict[str, Any]:
    return _finish(
        {
            "company_id": bill.company_id,
            "company_name": bill.company_name,
            "ledger_name": bill.ledger_name,
            "bill_ref": bill.bill_ref,
            "bill_type": bill.bill_type,
            "bill_date": bill.bill_date,
            "due_date": bill.due_date,
            "credit_period": bill.credit_period,
            "opening_amount": money(bill.opening_amount),
            "closing_amount": money(bill.closing_amount),
            "overdue_days": bill.overdue_days,
            "is_advance": bill.is_advance,
            "status": bill.status,
            "snapshot_at": snapshot_at,
            "raw_json": bill.raw_data,
            "is_deleted": False,
        }
    )


def normalize_voucher(
    voucher: Voucher,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (voucher row, ledger entry rows, inventory entry rows)."""
    row = _finish(
        {
            "company_id": voucher.company_id,
            "company_name": voucher.company_name,
            "tally_guid": voucher.tally_guid,
            "voucher_type": voucher.voucher_type,
            "voucher_number": voucher.voucher_number,
            "date": voucher.date,
            "effective_date": voucher.effective_date,
            "reference": voucher.reference,
            "reference_date": voucher.reference_date,
            "party_ledger_name": voucher.party_ledger_name,
            "narration": voucher.narration,
            "alter_id": voucher.alter_id,
            "master_id": voucher.master_id,
            "remote_id": voucher.remote_id,
            "vch_key": voucher.vch_key,
            "voucher_key": voucher.voucher_key,
            "persisted_view": voucher.persisted_view,
            "is_cancelled": voucher.is_cancelled,
            "is_optional": voucher.is_optional,
            "is_invoice": voucher.is_invoice,
            "is_post_dated": voucher.is_post_dated,
            "total_debit": money(voucher.total_debit),
            "total_credit": money(voucher.total_credit),
            "ledger_entry_count": len(voucher.ledger_entries),
            "inventory_entry_count": len(voucher.inventory_entries),
            "raw_json": voucher.raw_data,
            "is_deleted": voucher.is_deleted,
        }
    )
    common = {
        "company_id": voucher.company_id,
        "company_name": voucher.company_name,
        "voucher_guid": voucher.tally_guid,
        "voucher_date": voucher.date,
        "voucher_type": voucher.voucher_type,
        "voucher_number": voucher.voucher_number,
        "is_deleted": voucher.is_deleted,
    }
    ledger_rows = [
        _finish(
            {
                **common,
                "line_no": entry.line_no,
                "ledger_name": entry.ledger_name,
                "amount": money(entry.amount),
                "debit": money(entry.debit),
                "credit": money(entry.credit),
                "is_deemed_positive": entry.is_deemed_positive,
                "is_party_ledger": entry.is_party_ledger,
                "bill_allocations_json": [a.model_dump(mode="json") for a in entry.bill_allocations]
                or None,
                "extra_json": entry.extra,
            }
        )
        for entry in voucher.ledger_entries
    ]
    inventory_rows = [
        _finish(
            {
                **common,
                "line_no": entry.line_no,
                "stock_item_name": entry.stock_item_name,
                "quantity": money(entry.quantity),
                "unit": entry.unit,
                "actual_quantity": money(entry.actual_quantity),
                "billed_quantity": money(entry.billed_quantity),
                "rate": money(entry.rate),
                "rate_unit": entry.rate_unit,
                "amount": money(entry.amount),
                "discount": money(entry.discount),
                "godown_name": entry.godown_name,
                "batch_name": entry.batch_name,
                "accounting_ledger": entry.accounting_ledger,
                "is_deemed_positive": entry.is_deemed_positive,
                "extra_json": entry.extra,
            }
        )
        for entry in voucher.inventory_entries
    ]
    return row, ledger_rows, inventory_rows
