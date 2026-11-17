"""A report that is in Xero is deleted only after its entries come out of Xero (owner, 2026-11-17).

``take_out_of_xero(report)`` runs inside the DELETE request, before the delete itself:

- nothing recorded for the company's current organisation (the old system's reports, another
  organisation's): nothing to find in Xero - ``(False, None)``, and the delete goes ahead as before (the app
  warned that those entries stay in Xero);
- otherwise a Xero token (reconnect 400, no token service 503), then the report is CLAIMED -
  ``publishing_status = "publishing"`` under ``SELECT ... FOR UPDATE NOWAIT`` - so a publish, an edit
  or a second delete is refused while Xero is being emptied;
- ``xero/unpublish.check_removable`` (read-only): any refusal blocks the delete with the list, and
  nothing in Xero is touched;
- ``remove_all``: a refusal part-way keeps the report and marks it out of Xero
  (``mark_diverged_from_xero``), so a republish makes again what came out.

On success the report is left claimed (``(True, prior status)``): the caller deletes it while
claimed and calls ``release`` only if that delete fails.
"""

from __future__ import annotations

import logging

from django.db import DatabaseError, transaction
from minty_db.models import Entity, Report

from core.xero_tokens import XeroTokenUnavailable
from petty_cash.services import publish
from petty_cash.services.report_actions import (
    PUBLISHING,
    STILL_PUBLISHING_DELETE,
    ReportActionRefused,
)
from petty_cash.services.report_edits import mark_diverged_from_xero
from petty_cash.xero import publish_record
from petty_cash.xero import unpublish as xero_unpublish
from petty_cash.xero.steps import Run

logger = logging.getLogger("petty-cash-api")

KEPT = ("Xero didn't let me remove this report's entries, so I've kept the report. "
        "Sort these out in Xero, then delete it again.")
NO_TOKEN_SERVICE = "I couldn't reach Xero to remove this report's entries. Try again in a moment."


def take_out_of_xero(report: Report) -> tuple[bool, str | None]:
    """Empty Xero of the report's recorded entries. ``(True, prior publishing_status)`` when the
    report is now claimed and empty in Xero; ``(False, None)`` when nothing was recorded. Raises
    ``ReportActionRefused``."""
    entity = Entity.objects.get(id=report.entity_id)
    record = publish_record.load_record(report.id, entity.xero_org_id)
    found = xero_unpublish.entries(report, record)
    if not found:
        return False, None
    try:
        token = publish.xero_token(entity.id)
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            raise ReportActionRefused(publish.RECONNECT, 400, reconnect=True) from None
        raise ReportActionRefused(NO_TOKEN_SERVICE, 503) from None

    prior = _claim(report)
    diverged = False
    try:
        run = Run(entity=entity, access_token=token.access_token, org=str(token.xero_org_id or entity.xero_org_id),
                  currency=None)
        reasons, gone = xero_unpublish.check_removable(run, report, found)
        if reasons:
            logger.info("Unpublish: report %s kept - Xero would refuse %s entr(y/ies)", report.id, len(reasons))
            raise ReportActionRefused(KEPT, 409, reasons=reasons)
        refused = xero_unpublish.remove_all(run, report, found, gone)
        if refused:
            diverged = True
            raise ReportActionRefused(KEPT, 409, reasons=[refused])
    except BaseException:
        release(report.id, prior, diverged=diverged)
        raise
    logger.info("Unpublish: report %s is out of Xero (%s entr(y/ies))", report.id, len(found))
    return True, prior


def _claim(report: Report) -> str | None:
    try:
        with transaction.atomic():
            locked = Report.objects.select_for_update(nowait=True).get(id=report.id)
            if locked.publishing_status == PUBLISHING:
                raise ReportActionRefused(STILL_PUBLISHING_DELETE, 409)
            prior = locked.publishing_status
            locked.publishing_status = PUBLISHING
            locked.save(update_fields=["publishing_status"])
            return prior
    except DatabaseError as exc:  # NOWAIT: a publish start or another delete holds the row
        logger.warning("Unpublish: report %s is locked: %s", report.id, exc)
        raise ReportActionRefused(STILL_PUBLISHING_DELETE, 409) from None


def release(report_id, prior: str | None, *, diverged: bool = False) -> None:
    """Hand the report back: its prior status, and out of Xero when some entries already came out."""
    try:
        with transaction.atomic():
            locked = Report.objects.select_for_update().filter(id=report_id).first()
            if locked is None:
                return
            locked.publishing_status = prior
            fields = ["publishing_status"]
            if diverged:
                mark_diverged_from_xero(locked)
                fields += ["xero_integrated", "status"]
            locked.save(update_fields=fields)
    except DatabaseError:
        logger.exception("Unpublish: could not hand report %s back (status %s)", report_id, prior)
        raise
