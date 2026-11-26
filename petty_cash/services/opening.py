"""The report wizard's Opening step - Flask's ``blueprints/report/services/opening.py``, ported.

Flask's rules, quirks included: the opening chains off the previous report WHATEVER its status
(its closing balance, else its counted cash); a FIRST report (nothing submitted before) opens at,
and is saved at, the onboarding balance and may not be dated before onboarding's start date; a
save on a draft does not recompute its closing balance; Save & Next moves an advanced draft back
to Sales. A new day goes one at a time after the last submitted report. "Today" is Hong Kong's.

Two people starting the same day (owner's decisions doc R1, 2026-11-19): the first creates the report;
the second is refused with ``REPORT_STARTED`` and nothing of theirs is written - whether the report
was there when the save began (their page was read before it existed) or it lands between the
check and the insert (the ``report_entity_date_key`` unique key turns the insert away).

The starting cash (the user, 2026-11-26): onboarding may skip the opening balance, which then stays
NOT SET (``start_date`` and ``opening_balance`` NULL - never 0). A company with no report and nothing
set may not start a report (``OPENING_NOT_SET``); ``set_first_opening`` saves the date and the amount
first. Two people at once: the decision is made on the LOCKED settings row, so the second is told
what the first saved (``OpeningAlreadySet``) and overwrites nothing.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from minty_db.enums import ReportStatus
from minty_db.models import EntityPettycashSettings, Report

from petty_cash.days import today
from petty_cash.money import cents
from petty_cash.services.reports import (
    NOT_SENT,
    actual_cash_total,
    check_version,
    find_draft,
    first_report_defaults,
    future_date_error,
    last_submitted,
    live_drafts,
    log_history,
    mark_saved,
    previous_report,
    submitted_report_for,
    update_draft_progress,
)

NEGATIVE = "Hmm, it looks like your cash balance is negative. Could you fix that first?"
REPORT_STARTED = "A report for this date has already been started"
OPENING_NOT_SET = "Set your starting cash before your first report."
OPENING_SET = "The starting cash was just set by someone else."
OPENING_TOO_LATE = "Your first report has already started, so the starting cash can't be set here."


class OpeningRefused(Exception):
    """A save the date rules refuse, with the sentence and where the app goes (``to``)."""

    def __init__(self, message: str, *, to: str = "dashboard"):
        super().__init__(message)
        self.message = message
        self.to = to


class ReportStarted(Exception):
    """Someone else started the day's report first (R1): ``report`` is theirs."""

    def __init__(self, report: Report):
        super().__init__(REPORT_STARTED)
        self.message = REPORT_STARTED
        self.report = report


class OpeningAlreadySet(Exception):
    """Someone set the starting cash first: what they saved."""

    def __init__(self, start_date, opening_balance: Decimal):
        super().__init__(OPENING_SET)
        self.start_date = start_date
        self.opening_balance = opening_balance


class OpeningTooLate(Exception):
    """The company already has a report: the starting cash is the first report's, too late here."""


def opening_not_set(entity_id) -> bool:
    """No start date and no report at all: the first report must ask for the starting cash. A
    company that already reports (older ones have no start date) is never asked."""
    if first_report_defaults(entity_id)[1] is not None:
        return False
    return not Report.objects.filter(entity_id=entity_id).exists()


@transaction.atomic
def set_first_opening(entity_id, start_date, opening_balance: Decimal) -> None:
    """Save the starting cash, once. The row is created if missing (``entity_id`` is its key, so two
    creates cannot both land), then locked: the check is made on what the lock sees."""
    EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
    row = EntityPettycashSettings.objects.select_for_update().get(entity_id=entity_id)
    if row.start_date is not None:
        raise OpeningAlreadySet(row.start_date, cents(row.opening_balance))
    if Report.objects.filter(entity_id=entity_id).exists():
        raise OpeningTooLate()
    row.opening_balance = opening_balance
    row.start_date = start_date
    row.opening_skipped_at = None
    row.save(update_fields=["opening_balance", "start_date", "opening_skipped_at"])


def first_report_date_error(day, today_, entity_id) -> str | None:
    problem = future_date_error(day, today_)
    if problem:
        return problem
    floor = first_report_defaults(entity_id)[1]
    if floor and day < floor:
        return f"Transaction date must be on or after your onboarding date ({floor})."
    return None


def _sequence_error(entity_id, day, today_) -> str | None:
    """After a submitted report only the next day; before any, the first-report rule."""
    last = last_submitted(entity_id)
    if last is not None:
        expected = last.transaction_date + timedelta(days=1)
        if expected > today_:
            return f"Transaction date cannot be in the future. You can only create reports for dates up to {today_}."
        if day != expected:
            return (
                f"Reports go one day at a time - this one needs to be {expected}, "
                f"the day after your last submitted report ({last.transaction_date})."
            )
        return None
    return first_report_date_error(day, today_, entity_id)


def new_report_date_problem(entity_id, day) -> str | None:
    """The date rules a new day's report must pass. The sentence to show, or None."""
    today_ = today()
    return future_date_error(day, today_) or _sequence_error(entity_id, day, today_)


def first_report_opening(entity_id, day) -> Decimal | None:
    """The onboarding balance when ``day``'s report is the company's FIRST (onboarding gave a start
    date and nothing is dated before ``day``), else None - read on the server, never trusted."""
    opening, start = first_report_defaults(entity_id)
    if start and not Report.objects.filter(entity_id=entity_id, transaction_date__lt=day).exists():
        return opening
    return None


def _closing(report) -> Decimal:
    return cents(report.closing_balance) if report and report.closing_balance is not None else Decimal("0.00")


def _safebox(report) -> Decimal:
    return cents(report.safe_box_balance) if report and report.safe_box_balance is not None else Decimal("0.00")


def opening_state(entity_id, selected_date) -> dict:
    """What the Opening page shows for a day: the day's draft when there is one, else what the day
    opens from. With no ``selected_date`` the latest LIVE draft of the last 7 days stands in (a
    stale one, dated before the last submitted day, never does)."""
    draft = find_draft(entity_id, selected_date) if selected_date else None
    if draft is None and not selected_date:
        draft = (
            live_drafts(entity_id)
            .filter(transaction_date__gte=today() - timedelta(days=7))
            .order_by("-transaction_date")
            .first()
        )

    if draft is not None:
        yesterday = previous_report(entity_id, draft.transaction_date)
        withdrawal = draft.cash_addition_type or "personal"
        return {
            "draft": draft,
            "opening_balance": cents(draft.opening_balance),
            "yesterday_closing_balance": _closing(yesterday),
            "safebox_balance": _safebox(yesterday),
            "cash_addition": cents(draft.cash_addition),
            "withdrawal": withdrawal,
            "next_transaction_date": draft.transaction_date,
        }

    if selected_date:
        last = last_submitted(entity_id, before=selected_date)
        previous = previous_report(entity_id, selected_date)
    else:
        last = previous = Report.objects.filter(entity_id=entity_id).order_by("-transaction_date").first()

    if previous is not None:
        if previous.closing_balance is not None:
            opening = cents(previous.closing_balance)
        else:
            counted = actual_cash_total(previous)
            opening = counted if counted is not None else Decimal("0.00")
    else:
        opening = first_report_defaults(entity_id)[0]

    if selected_date:
        next_day = selected_date
    else:
        next_day = (last.transaction_date + timedelta(days=1)) if last else first_report_defaults(entity_id)[1]

    is_first = last is None
    return {
        "draft": None,
        "opening_balance": opening,
        # A first report shows (and saves) the opening worked out above - the onboarding one.
        "yesterday_closing_balance": (
            cents(last.closing_balance)
            if last is not None and last.closing_balance is not None
            else (opening if is_first else Decimal("0.00"))
        ),
        "safebox_balance": _safebox(last),
        "cash_addition": Decimal("0.00"),
        "withdrawal": "personal",
        "next_transaction_date": next_day,
    }


def starting_cash_balance(state: dict) -> Decimal:
    """The "Starting Cash Balance" - and what is saved as the opening balance: yesterday's closing
    when there is one, else the draft's own opening, else 0."""
    draft = state["draft"]
    return state["yesterday_closing_balance"] or (
        cents(draft.opening_balance) if draft is not None and draft.opening_balance else Decimal("0.00")
    )


def _history_number(value: Decimal) -> str:
    """Flask wrote these amounts as Python floats ("1000.0") in the history line."""
    return str(float(value))


@transaction.atomic
def save_opening(entity_id, user, day, opening_balance: Decimal, cash_addition: Decimal, withdrawal,
                 action_type: str, opened=NOT_SENT) -> Report:
    """Write the Opening step onto the day's draft, creating it (after the date rules) when there
    is none. ``opened`` is the ``edit_version`` the page was read at - None when the day had no
    report then. Raises ``OpeningRefused``, ``ReportStarted`` (R1) and ``StepAlreadySaved`` (R2).
    One transaction, history included."""
    opening_balance, cash_addition = cents(opening_balance), cents(cash_addition)
    adjusted = opening_balance + cash_addition
    draft = find_draft(entity_id, day)
    created = draft is None
    if draft is not None:
        if opened is None:
            raise ReportStarted(draft)
        draft = Report.objects.select_for_update().get(id=draft.id)
        check_version(draft, opened)
        draft.created_by = user.id  # Flask's ``uploaded_by = username``: the last editor
        draft.opening_balance = opening_balance
        draft.cash_addition = cash_addition
        draft.adjusted_opening_balance = adjusted
        draft.next_transaction_date = day + timedelta(days=1)
    else:
        if submitted_report_for(entity_id, day):
            raise OpeningRefused(f"A report for {day} already exists. Please delete it first.")
        problem = _sequence_error(entity_id, day, today())
        if problem:
            raise OpeningRefused(problem)
        # Every Python-side default Flask's Report(...) applied, spelled out (rule 0).
        draft = Report(
            entity_id=entity_id,
            transaction_date=day,
            next_transaction_date=day + timedelta(days=1),
            status=ReportStatus.DRAFT,
            opening_balance=opening_balance,
            cash_addition=cash_addition,
            adjusted_opening_balance=adjusted,
            cashsale_total=Decimal("0.00"),
            nocashsale_total=Decimal("0.00"),
            total_sales=Decimal("0.00"),
            expense_total=Decimal("0.00"),
            bank_deposit=Decimal("0.00"),
            discrepancy_amount=Decimal("0.00"),
            xero_integrated=False,
            closing_balance=adjusted,
            current_section="opening",
            completed_sections=[],
            created_by=user.id,
            cash_addition_type=withdrawal,
            edit_version=0,
        )
    if withdrawal:
        draft.cash_addition_type = withdrawal
    update_draft_progress(draft, action_type, "opening", "sales")
    mark_saved(draft, user)
    if created:
        try:
            with transaction.atomic():
                draft.save()
        except IntegrityError:
            # Someone else's report for the day landed after the check (report_entity_date_key).
            theirs = Report.objects.filter(entity_id=entity_id, transaction_date=day).first()
            if theirs is None:
                raise
            raise ReportStarted(theirs) from None
    else:
        draft.save()
    log_history(
        draft,
        user,
        action="created" if created else "updated",
        field_changed="opening_entry",
        old_value="None" if created else "previous_opening_state",
        new_value=(
            f"Opening: {_history_number(opening_balance)}, Addition: {_history_number(cash_addition)}, "
            f"Adjusted: {_history_number(adjusted)}"
        ),
    )
    return draft
