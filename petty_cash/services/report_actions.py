"""What can be done to a report after it is submitted - reopening it as a draft and deleting it -
Flask's ``blueprints/report/services/report_actions.py``, ported.

Flask's rules: only the latest report (by day, any status) may be reopened or deleted; reopening
needs ``REPORT_EDIT_OWN`` and keeps the report's figures and its Xero markers (so a republish can
warn); deleting needs ``can_delete_report`` and takes the report's lines, receipts, sales, counts,
history and Xero publish record with it; both refresh the company's cash in stock. A draft can't be
reopened, nothing is reopened or deleted while Xero is publishing it, and a deleted report's
receipts leave storage only after the commit. The owner's rule (2026-10-15): a report already in
Xero CAN be deleted - the app warns, reading ``was_published``.

The rows are deleted one table at a time: Postgres would cascade them from ``report``, but the
SQLite test tables carry no foreign keys, and naming each table says what a delete takes.
"""

from __future__ import annotations

import logging

from django.db import transaction
from minty_db.enums import ReportStatus
from minty_db.models import (
    Attachment,
    Report,
    ReportCashCount,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    ReportSale,
    XeroBankTransaction,
    XeroBankTransfer,
    XeroReportSync,
)

from core.policy import Permission, can_delete_report, can_edit_report, has_permission
from petty_cash.files import storage
from petty_cash.services.denominations import refresh_cash_stock
from petty_cash.services.reports import is_stale_draft

logger = logging.getLogger("petty-cash-api")

PUBLISHING = "publishing"
NEWER_REOPEN = "You can only edit the most recent report. Delete or edit the newer reports first."
NEWER_DELETE = "I can only delete the most recent report, and there's a newer one after this."
IN_XERO_RIGHT = "Only an accountant or admin can delete a report that's in Xero."
STILL_PUBLISHING_DELETE = "Xero is still publishing this report. Wait until it finishes before deleting it."


class ReportActionRefused(Exception):
    """A reopen or delete the rules refuse: the sentence and the HTTP status to answer with, and any
    key the app routes on (``reasons``: what Xero refused; ``reconnect``)."""

    def __init__(self, message: str, status: int, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def was_published(report: Report) -> bool:
    """Whether the report has ever gone to Xero (fully or not): its Xero entries may exist."""
    return bool(report.xero_integrated) or report.publishing_status not in (None, "unpublished")


def newer_exists(report: Report) -> bool:
    return Report.objects.filter(entity_id=report.entity_id, transaction_date__gt=report.transaction_date).exists()


def reopen_report(report: Report, user) -> Report:
    """Turn the latest submitted (or published) report back into a draft at Opening, its figures
    and Xero markers kept."""
    # a cashier reopens only the reports they created; a shop manager and up anyone's (owner, 2026-11-17)
    if not can_edit_report(user, report):
        raise ReportActionRefused("You do not have permission to edit this report.", 403)
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        if locked.status == ReportStatus.DRAFT:
            raise ReportActionRefused("This report is already a draft. Carry on with the Daily Closing.", 409)
        if locked.publishing_status == PUBLISHING:
            raise ReportActionRefused("Xero is still publishing this report. Wait until it finishes, then edit it.",
                                      409)
        if newer_exists(locked):
            raise ReportActionRefused(NEWER_REOPEN, 400)
        locked.status = ReportStatus.DRAFT
        locked.current_section = "opening"
        locked.completed_sections = []
        locked.save(update_fields=["status", "current_section", "completed_sections"])
        refresh_cash_stock(locked.entity_id)
    logger.info("Report %s reopened as a draft (was_published=%s)", locked.id, bool(locked.xero_integrated))
    return locked


def may_delete(user, report: Report) -> bool:
    """The delete right, and for a report that has been to Xero the publish right too: its entries
    come out of Xero with it (owner, 2026-11-17: accountant and up)."""
    return can_delete_report(user, report) and (
        not was_published(report) or has_permission(user, Permission.REPORT_PUBLISH, report.entity_id))


def _refuse_undeletable(report: Report, *, claimed: bool = False) -> bool:
    """Publishing (unless this delete claimed it) is 409, a newer report 400. Whether it is a stale
    draft: a draft dated before the last submitted day may go though it is not the latest - nothing
    can finish it, and nothing after it depends on it (owner, 2026-10-26)."""
    if report.publishing_status == PUBLISHING and not claimed:
        raise ReportActionRefused(STILL_PUBLISHING_DELETE, 409)
    stale = is_stale_draft(report)
    if newer_exists(report) and not stale:
        raise ReportActionRefused(NEWER_DELETE, 400)
    return stale


def delete_report(report: Report, user) -> None:
    """Delete the latest report with everything hanging off it; its receipt files go after the
    commit, so a failed delete keeps the report AND its files. A report in Xero has its entries
    taken out of Xero first, and is kept if Xero refuses any (``services/unpublish.py``)."""
    from petty_cash.services import unpublish  # it imports this module

    if not can_delete_report(user, report):
        raise ReportActionRefused("You are not authorized to delete this report.", 403)
    if not may_delete(user, report):
        raise ReportActionRefused(IN_XERO_RIGHT, 403)
    _refuse_undeletable(report)
    claimed, prior = unpublish.take_out_of_xero(report) if was_published(report) else (False, None)
    try:
        _delete(report, claimed=claimed)
    except BaseException:
        if claimed:
            # its entries are out of Xero already: back as a report no longer in Xero
            unpublish.release(report.id, prior, diverged=True)
        raise


def _delete(report: Report, *, claimed: bool) -> None:
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        stale = _refuse_undeletable(locked, claimed=claimed)

        report_id, entity_id = locked.id, locked.entity_id
        if not stale:  # the sequence has moved on past a stale day: the report before keeps its link
            _clear_previous_next_date(locked)

        line_ids = list(ReportExpense.objects.filter(report_id=report_id).values_list("id", flat=True))
        attachment_ids = list(
            ReportExpenseAttachment.objects.filter(report_expense_id__in=line_ids).values_list("attachment_id", flat=True)
        )
        ReportExpenseAttachment.objects.filter(report_expense_id__in=line_ids).delete()
        ReportExpense.objects.filter(id__in=line_ids).delete()
        # attachment rows no other line still links
        orphans = list(
            Attachment.objects.filter(id__in=attachment_ids).exclude(
                id__in=ReportExpenseAttachment.objects.filter(attachment_id__in=attachment_ids).values("attachment_id")
            )
        )
        keys = [a.file_path for a in orphans if a.file_path]
        Attachment.objects.filter(id__in=[a.id for a in orphans]).delete()
        ReportSale.objects.filter(report_id=report_id).delete()
        ReportCashCount.objects.filter(report_id=report_id).delete()
        ReportHistory.objects.filter(report_id=report_id).delete()
        XeroReportSync.objects.filter(report_id=report_id).delete()
        XeroBankTransfer.objects.filter(sync_report_id=report_id).delete()
        XeroBankTransaction.objects.filter(sync_report_id=report_id).delete()
        Report.objects.filter(id=report_id).delete()
        refresh_cash_stock(entity_id)
        transaction.on_commit(lambda: storage.delete_files(keys))
    logger.info("Report %s deleted with %s receipt file(s)", report_id, len(keys))


def _clear_previous_next_date(report: Report) -> None:
    previous = (
        Report.objects.filter(entity_id=report.entity_id, transaction_date__lt=report.transaction_date)
        .order_by("-transaction_date")
        .first()
    )
    if previous is not None and previous.next_transaction_date is not None:
        Report.objects.filter(id=previous.id).update(next_transaction_date=None)
