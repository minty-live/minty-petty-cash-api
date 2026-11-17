"""Taking a report's entries back out of Xero before the report is deleted (owner, 2026-11-17).

Only what the report's publish recorded (``publish_record``) - Minty never searches Xero for entries
it did not make. Two passes:

1. ``check_removable``: read-only. Every recorded entry is read from Xero; a reconciled transaction
   or transfer, an invoice with a payment or credit, or a day on or before the organisation's lock
   date is refused, with a sentence naming the entry and what to do in Xero. An entry Xero no
   longer has (404, DELETED, VOIDED) counts as already gone.
2. ``remove_all``: the publish's own removals (``steps._delete_transaction`` / ``_delete_transfer`` /
   ``_void_invoice``), in the publish's order, each forgotten from the record the moment Xero says
   yes. The first refusal stops the run.

The caller (``services/report_actions.py::delete_report``) blocks the delete on any refusal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import requests
from minty_db.models import Report, ReportExpense

from petty_cash.services.cash import _money
from petty_cash.xero import client, publish_record, steps
from petty_cash.xero.errors import translate_xero_error

logger = logging.getLogger("petty-cash-api")

#: The publish's order (``steps.publish_report``).
ORDER = ("withdrawal_from", "invoices", "expenses", "deposit", "discrepancy")
LABELS = {
    "withdrawal_from": "Addition to cash drawer",
    "invoices": "Cash sales invoice",
    "deposit": "Bank deposit",
    "discrepancy": "Cash difference",
}
RECONCILED = "it's reconciled in Xero. Unreconcile it in Xero first."
PAID = "it has a payment in Xero. Remove the payment in Xero first."
UNREACHABLE = "I couldn't reach Xero to check it. Try again in a moment."
LOCKED = "This day is on or before Xero's lock date ({day}). Move the lock date in Xero first."
NO_LOCK_DATE = "I couldn't read Xero's lock date. Try again in a moment."
GONE = {"DELETED", "VOIDED"}


@dataclass(frozen=True)
class Entry:
    """One recorded Xero object: the report's part (and line), its id and type, and its name here."""

    module: str
    source_id: str | None
    xero_id: str
    object_type: str
    label: str


def entries(report: Report, record: dict) -> list[Entry]:
    """What the report's publish recorded, in the publish's order."""
    found: list[Entry] = []
    objects = record.get("objects") or {}
    for module in ORDER:
        if module == "expenses":
            ids = publish_record.recorded_expense_ids(record)
            lines = {str(line.id): line for line in ReportExpense.objects.filter(id__in=list(ids))}
            for line_id, xero_id in ids.items():
                line = lines.get(line_id)
                label = (f"Expense {line.item or line.description or 'line'}, {_money(report, line.amount)}"
                         if line else "An expense no longer on this report")
                found.append(Entry("expenses", line_id, xero_id, "BANK_TRANSACTION", label))
            continue
        entry = publish_record.recorded_entry(record, module)
        if entry:
            kind = entry.get("type") or ("INVOICE" if module == "invoices" else "BANK_TRANSACTION")
            found.append(Entry(module, None, entry["id"], kind, LABELS[module]))
    unknown = set(objects) - set(ORDER)
    if unknown:
        logger.warning("Unpublish: report %s records parts this code does not know: %s", report.id, sorted(unknown))
    return found


def _xero_date(value) -> date | None:
    """Xero's ``/Date(1572480000000+0000)/`` or an ISO date, as a date."""
    if not value:
        return None
    match = re.match(r"/Date\((-?\d+)", str(value))
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=UTC).date()
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _lock_reason(run: steps.Run, day: date) -> str | None:
    try:
        response = client.get("Organisation", run.access_token, run.org)
    except requests.RequestException as exc:
        logger.error("Unpublish: reading the organisation failed: %s", exc)
        return NO_LOCK_DATE
    if response.status_code != 200:
        logger.error("Unpublish: Xero refused the organisation read: %s", response.status_code)
        return translate_xero_error(response.status_code, response.text) if response.status_code == 401 \
            else NO_LOCK_DATE
    try:
        organisation = (response.json().get("Organisations") or [{}])[0]
    except (ValueError, AttributeError, IndexError):
        return NO_LOCK_DATE
    # both locks, the later one: Xero's period lock binds everyone but advisers, the year-end lock
    # everyone - the safe reading, as a refused delete only asks for a lock date to move
    locks = [d for d in (_xero_date(organisation.get("PeriodLockDate")),
                         _xero_date(organisation.get("EndOfYearLockDate"))) if d]
    if locks and day <= max(locks):
        lock = max(locks)
        return LOCKED.format(day=f"{lock.day} {lock:%b %Y}")
    return None


_COLLECTIONS = {"BANK_TRANSACTION": "BankTransactions", "BANK_TRANSFER": "BankTransfers", "INVOICE": "Invoices"}


def _entry_reason(run: steps.Run, entry: Entry) -> tuple[bool, str | None]:
    """``(gone, why)``: already out of Xero, or the sentence that refuses its removal (None: removable)."""
    collection = _COLLECTIONS.get(entry.object_type)
    if collection is None:
        logger.error("Unpublish: entry %s has an unknown type %r", entry.xero_id, entry.object_type)
        return False, f"{entry.label}: {UNREACHABLE}"
    try:
        response = client.get(f"{collection}/{entry.xero_id}", run.access_token, run.org)
    except requests.RequestException as exc:
        logger.error("Unpublish: reading %s %s failed: %s", collection, entry.xero_id, exc)
        return False, f"{entry.label}: {UNREACHABLE}"
    if response.status_code == 404:
        return True, None
    if response.status_code != 200:
        logger.error("Unpublish: Xero refused reading %s %s: %s", collection, entry.xero_id, response.status_code)
        return False, f"{entry.label}: {translate_xero_error(response.status_code, response.text)}."
    try:
        found = (response.json().get(collection) or [{}])[0]
    except (ValueError, AttributeError, IndexError):
        found = {}
    if str(found.get("Status") or "").upper() in GONE:
        return True, None
    if entry.object_type == "BANK_TRANSACTION" and found.get("IsReconciled"):
        return False, f"{entry.label}: {RECONCILED}"
    if entry.object_type == "BANK_TRANSFER" and (found.get("FromIsReconciled") or found.get("ToIsReconciled")):
        return False, f"{entry.label}: {RECONCILED}"
    if entry.object_type == "INVOICE" and (float(found.get("AmountPaid") or 0) > 0
                                           or float(found.get("AmountCredited") or 0) > 0
                                           or found.get("Payments")):
        return False, f"{entry.label}: {PAID}"
    return False, None


def check_removable(run: steps.Run, report: Report, found: list[Entry]) -> tuple[list[str], set[Entry]]:
    """Read-only. ``(reasons, gone)``: every refusal's sentence (none: all may be removed) and the
    entries Xero no longer has."""
    reasons: list[str] = []
    gone: set[Entry] = set()
    locked = _lock_reason(run, report.transaction_date)
    if locked:
        reasons.append(locked)
    for entry in found:
        already, why = _entry_reason(run, entry)
        if already:
            gone.add(entry)
        elif why:
            reasons.append(why)
    return reasons, gone


def remove_all(run: steps.Run, report: Report, found: list[Entry], gone: set[Entry]) -> str | None:
    """Remove each entry from Xero, forgetting it from the record at once. None when all are out;
    else the first refusal's sentence (what came out before it stays forgotten)."""
    for entry in found:
        if entry in gone:
            ok, why = True, None
        elif entry.object_type == "BANK_TRANSFER":
            ok, why = steps._delete_transfer(run, entry.xero_id)
        elif entry.object_type == "INVOICE":
            ok, why = steps._void_invoice(run, entry.xero_id)
        else:
            ok, why = steps._delete_transaction(run, entry.xero_id)
        if not ok:
            logger.warning("Unpublish: Xero refused removing %s %s of report %s: %s", entry.object_type,
                           entry.xero_id, report.id, why)
            return f"{entry.label}: {why or 'Xero refused it'}."
        publish_record.forget_object(report.id, entry.module, source_id=entry.source_id)
        logger.info("Unpublish: %s %s of report %s is out of Xero", entry.object_type, entry.xero_id, report.id)
    return None
