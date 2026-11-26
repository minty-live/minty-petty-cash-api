"""The report wizard's Opening step - Flask's ``petty_cash_api/routes/opening.py``.

- ``GET /reports/opening?transaction_date=``: the day's draft, or what the day opens from.
- ``PUT /reports/opening`` ``{transaction_date, cash_addition, withdrawal, action, edit_version}``: the
  save. The opening balance is the server's own "Starting Cash Balance" (a first report's: the
  onboarding balance) - never taken from the client. ``edit_version`` is the read's (null when the
  day had no report then).

A date rule's refusal is 409 ``{error, to: "dashboard"}``; a negative balance 409
``{error, to: "opening"}``; bad input 422. Two people (owner's decisions doc R1/R2, 2026-11-19):
the day's report started by someone else first is 409 ``{error: "A report for this date has already
been started", report_id, to}``; a save opened from an older version is 409 ``{error: "This step was
already saved by {name} at {time}", saved, next: "sales"}``. ``REPORT_EDIT_OWN`` throughout.

- ``PUT /opening-balance`` ``{start_date, opening_balance}`` (the user, 2026-11-26): the company's
  starting cash, asked before its FIRST report when onboarding skipped it. Saved once: a second
  person is 409 ``{code: "OPENING_SET", start_date, opening_balance}`` (what the first saved); a
  company that already has a report is 409 ``REPORT_EXISTS``. Until it is set, starting a first
  report is 409 ``{code: "OPENING_NOT_SET", to: "dashboard"}``.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from minty_db.enums import ReportStatus
from minty_db.models import CurrencyInfo, Entity
from ninja import Router

from core.exceptions import Refused
from petty_cash.days import parse_day, today
from petty_cash.money import out
from petty_cash.routes._input import (
    BAD_DATE,
    action_type,
    already_saved,
    amount,
    json_body,
    opened_version,
    query_day,
    require_edit,
)
from petty_cash.services import opening as opening_service
from petty_cash.services.opening import (
    NEGATIVE,
    OPENING_NOT_SET,
    OPENING_SET,
    OPENING_TOO_LATE,
    OpeningAlreadySet,
    OpeningRefused,
    OpeningTooLate,
    ReportStarted,
    first_report_opening,
    new_report_date_problem,
    opening_state,
    save_opening,
    starting_cash_balance,
)
from petty_cash.services.reports import (
    StepAlreadySaved,
    currency_code,
    draft_payload,
    future_date_error,
    progress_payload,
    submitted_report_for,
)

router = Router()
logger = logging.getLogger(__name__)

# Owner 2026-10-21: never a silent "$" - a company with no currency picks one before any report.
NO_CURRENCY = "No currency is selected for this company. Choose one in Settings, under General, to start a report."


def opening_payload(entity_id, selected) -> dict:
    state = opening_state(entity_id, selected)
    draft = state["draft"]
    day = selected or state["next_transaction_date"] or today()
    cash_addition = state["cash_addition"] or None
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        # the version a save carries back (R2); null before the day has a report
        "edit_version": draft.edit_version if draft is not None else None,
        "starting_cash_balance": out(starting_cash_balance(state)),
        "yesterday_closing_balance": out(state["yesterday_closing_balance"]),
        "safebox_balance": out(state["safebox_balance"]),
        "cash_addition": out(cash_addition) if cash_addition is not None else None,
        "withdrawal": state["withdrawal"],
    }


@router.get("/reports/opening")
def opening_page(request):
    require_edit(request)
    return opening_payload(request.company.id, query_day(request, required=False))


def _valid_save(body) -> dict:
    if not isinstance(body, dict):
        raise Refused("Send the opening as JSON.", 422)
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    raw = body.get("cash_addition", 0)
    addition = amount(0 if raw is None else raw)
    if addition is None:
        raise Refused("Enter the cash added to the drawer as an amount of 0 or more.", 422)
    withdrawal = body.get("withdrawal")
    if withdrawal not in ("company", "personal", None):
        raise Refused("That withdrawal type doesn't look quite right to me.", 422)
    action = action_type(body)
    if action is None:
        raise Refused("Choose Save & Next or Save & Exit.", 422)
    return {"day": day, "cash_addition": addition, "withdrawal": withdrawal, "action_type": action,
            "opened": opened_version(body, nullable=True)}


@router.put("/reports/opening")
def save_opening_step(request):
    require_edit(request)
    entity_id = request.company.id
    save = _valid_save(json_body(request))
    day = save["day"]

    if not currency_code(entity_id):
        raise Refused(NO_CURRENCY, 409, to="dashboard")
    if opening_service.opening_not_set(entity_id):
        raise Refused(OPENING_NOT_SET, 409, code="OPENING_NOT_SET", to="dashboard")
    problem = new_report_date_problem(entity_id, day)
    if problem:
        raise Refused(problem, 409, to="dashboard")

    opening = starting_cash_balance(opening_state(entity_id, day))
    onboarding = first_report_opening(entity_id, day)
    if onboarding is not None:
        opening = onboarding
    if opening + save["cash_addition"] < 0:
        raise Refused(NEGATIVE, 409, to="opening")
    if submitted_report_for(entity_id, day):
        raise Refused(f"A report for {day} already exists. Please delete it first.", 409, to="dashboard")

    try:
        draft = save_opening(
            entity_id, request.auth_user, day, opening, save["cash_addition"], save["withdrawal"], save["action_type"],
            save["opened"],
        )
    except OpeningRefused as refused:
        raise Refused(refused.message, 409, to=refused.to) from None
    except ReportStarted as started:
        theirs = started.report
        to = (theirs.current_section or "opening") if theirs.status == ReportStatus.DRAFT else "dashboard"
        raise Refused(started.message, 409, report_id=str(theirs.id), to=to) from None
    except StepAlreadySaved as stale:
        raise already_saved(stale, opening_payload(entity_id, day), "sales") from None
    return progress_payload(draft, "sales" if save["action_type"] == "save_next" else "dashboard")



# --- the starting cash, before the first report (the user, 2026-11-26) -------------------------------

NO_START_DATE = "Choose the start date to continue."
NO_OPENING = "The cash in your petty cash box is still empty. Add the amount to continue."
BAD_OPENING = "Enter the cash in your petty cash box as 0 or more."


def _company_currency(entity_id) -> CurrencyInfo | None:
    currency_id = Entity.objects.filter(id=entity_id).values_list("currency_id", flat=True).first()
    return CurrencyInfo.objects.filter(id=currency_id).first() if currency_id else None


def _starting_cash(raw, currency: CurrencyInfo) -> Decimal:
    """A JSON number of 0 or more, in the currency's own decimal places (JPY: whole amounts) - the
    same rule as onboarding's save, so both writers agree. Blank is refused, never read as 0."""
    if raw is None:
        raise Refused(NO_OPENING, 422)
    number = amount(raw)
    if number is None:
        raise Refused(BAD_OPENING, 422)
    try:
        exact = Decimal(str(raw))
    except InvalidOperation:
        raise Refused(BAD_OPENING, 422) from None
    places = currency.decimal_places if currency.decimal_places is not None else 2
    if exact.normalize().as_tuple().exponent < -places:
        code = (currency.currency_code or "").strip()
        raise Refused(f"That amount has more decimal places than {code} allows ({places}).", 422)
    return exact.quantize(Decimal(1).scaleb(-places))


@router.put("/opening-balance")
def set_starting_cash(request):
    require_edit(request)
    entity_id = request.company.id
    body = json_body(request)
    if not isinstance(body, dict):
        raise Refused("Send the starting cash as JSON.", 422)
    currency = _company_currency(entity_id)
    if currency is None:
        raise Refused(NO_CURRENCY, 409, to="dashboard")
    day = parse_day(body.get("start_date"))
    if day is None:
        raise Refused(NO_START_DATE, 422)
    problem = future_date_error(day, today())
    if problem:
        raise Refused(problem, 422)
    balance = _starting_cash(body.get("opening_balance"), currency)

    try:
        opening_service.set_first_opening(entity_id, day, balance)
    except OpeningAlreadySet as already:
        raise Refused(
            OPENING_SET, 409, code="OPENING_SET", start_date=already.start_date.isoformat(),
            opening_balance=out(already.opening_balance),
        ) from None
    except OpeningTooLate:
        raise Refused(OPENING_TOO_LATE, 409, code="REPORT_EXISTS", to="dashboard") from None
    logger.info("starting cash set: start=%s amount=%s", day, balance)
    return {"start_date": day.isoformat(), "opening_balance": out(balance)}
