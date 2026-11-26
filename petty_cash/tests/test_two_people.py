"""Two people on one report (owner's decisions doc R1/R2, 2026-11-19).

R1: two people starting the same day - the first creates the report, the second is refused with
"A report for this date has already been started" and the report's id, and nothing of theirs is
written; a race the date check misses is turned away by ``report_entity_date_key``, never a 500.

R2: two people saving the same step - each step's read answers ``edit_version`` and its save
carries it back; the first save wins, a save from an older version is refused with who saved it and
when, the saved values and the next step. Expense lines are additive: they neither check nor move
the version. A save without ``edit_version`` (a page from before the check) is applied unchecked.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from minty_db.enums import EntityRole, SaleType
from minty_db.models import (
    AccountInfo,
    EntityAccountXero,
    EntitySaleSetting,
    Report,
    ReportCashCount,
    ReportExpense,
    ReportHistory,
    ReportSale,
    SaleInfo,
    UserEntity,
    XeroContactSync,
)

from petty_cash.days import hk, today
from petty_cash.files import storage
from petty_cash.services import opening as opening_service
from petty_cash.tests.conftest import headers, make_report, make_user, url
from petty_cash.tests.test_expenses import PNG

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)
STARTED = "A report for this date has already been started"


@pytest.fixture
def sam(shop):
    """A second person who may fill in the company's reports."""
    user = make_user("sam@test.com", first_name="Sam", last_name="Second")
    UserEntity.objects.create(user_id=user.id, entity_id=shop.entity.id, role=EntityRole.ADMIN, approved=True)
    return user


def _get(client, shop, step, user=None):
    return client.get(url(shop.entity.id, f"/reports/{step}"), {"transaction_date": D3.isoformat()},
                      headers=headers((user or shop.owner).id))


def _put(client, shop, step, body, user=None):
    return client.put(url(shop.entity.id, f"/reports/{step}"),
                      {"transaction_date": D3.isoformat(), "action": "next", **body},
                      content_type="application/json", headers=headers((user or shop.owner).id))


def _draft(shop, **fields):
    """A draft on Deposit: cash on hand 155 (100 + 20 + 50 - 15)."""
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120, "cashsale_total": 50,
              "expense_total": 15, "closing_balance": 155, "completed_sections": ["opening", "sales", "expenses"],
              "current_section": "deposit"}
    values.update(fields)
    return make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, **values)


def _saved_by(report, user):
    """Someone else's save, as a step save leaves the row: the next version, and who."""
    Report.objects.filter(id=report.id).update(edit_version=F("edit_version") + 1, saved_by_user_id=user.id)


def _sentence(report, name):
    row = Report.objects.get(id=report.id)
    return f"This step was already saved by {name} at {hk(row.updated_at):%H:%M}"


def _history(report) -> int:
    return ReportHistory.objects.filter(report_id=report.id).count()


# --- R1: starting the same day -------------------------------------------------------------------------


def test_the_opening_read_has_no_version_before_the_day_has_a_report(client, shop):
    assert _get(client, shop, "opening").json()["edit_version"] is None


def test_the_first_start_creates_the_report_at_version_one_saved_by_its_starter(client, shop):
    resp = _put(client, shop, "opening", {"cash_addition": 30, "withdrawal": "company", "edit_version": None})

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(entity_id=shop.entity.id, transaction_date=D3)
    assert row.edit_version == 1 and row.saved_by_user_id == shop.owner.id
    assert _get(client, shop, "opening").json()["edit_version"] == 1


def test_a_second_start_from_a_page_read_before_the_report_existed_is_turned_away(client, shop, sam):
    first = {"cash_addition": 30, "withdrawal": "personal", "edit_version": None}
    assert _put(client, shop, "opening", first).status_code == 200
    theirs = Report.objects.get(entity_id=shop.entity.id, transaction_date=D3)
    before = _history(theirs)

    resp = _put(client, shop, "opening", {"cash_addition": 999, "withdrawal": "company", "edit_version": None},
                user=sam)

    assert resp.status_code == 409
    assert resp.json() == {"error": STARTED, "report_id": str(theirs.id), "to": "sales"}
    row = Report.objects.get(id=theirs.id)
    assert row.cash_addition == Decimal("30.00") and row.cash_addition_type == "personal"
    assert row.edit_version == 1 and row.saved_by_user_id == shop.owner.id and _history(row) == before


def test_a_start_the_date_check_misses_is_turned_away_by_the_unique_key(client, shop, sam, monkeypatch):
    """The race: Sam's report for the day lands after this save looked for one (``find_draft``
    still says none), so the insert meets ``report_entity_date_key`` - a 409, never a 500."""
    theirs = make_report(shop.entity, D3, status="draft", created_by=sam.id, opening_balance=0, cash_addition=5,
                         current_section="opening", completed_sections=[])
    monkeypatch.setattr(opening_service, "find_draft", lambda *args, **kwargs: None)

    resp = _put(client, shop, "opening", {"cash_addition": 999, "edit_version": None})

    assert resp.status_code == 409, resp.content
    assert resp.json() == {"error": STARTED, "report_id": str(theirs.id), "to": "opening"}
    assert list(Report.objects.filter(entity_id=shop.entity.id).values_list("id", flat=True)) == [theirs.id]
    row = Report.objects.get(id=theirs.id)
    assert row.cash_addition == Decimal("5.00") and _history(row) == 0


# --- R2: the Opening step -----------------------------------------------------------------------------


def test_an_opening_save_from_an_older_version_is_refused_with_who_when_and_the_saved_values(client, shop, sam):
    draft = make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, opening_balance=0,
                        cash_addition=30, current_section="sales", completed_sections=["opening"])
    opened = _get(client, shop, "opening").json()["edit_version"]
    _saved_by(draft, sam)

    resp = _put(client, shop, "opening", {"cash_addition": 999, "edit_version": opened})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == _sentence(draft, "Sam Second")
    assert body["next"] == "sales"
    assert body["saved"] == _get(client, shop, "opening").json()
    assert body["saved"]["cash_addition"] == 30.0 and body["saved"]["edit_version"] == opened + 1
    assert Report.objects.get(id=draft.id).cash_addition == Decimal("30.00") and _history(draft) == 0


def test_a_save_on_the_current_version_applies_and_moves_the_version_on(client, shop, sam):
    draft = make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, opening_balance=0,
                        cash_addition=30, current_section="sales", completed_sections=["opening"])
    opened = _get(client, shop, "opening", user=sam).json()["edit_version"]

    resp = _put(client, shop, "opening", {"cash_addition": 40, "edit_version": opened}, user=sam)

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(id=draft.id)
    assert row.cash_addition == Decimal("40.00")
    assert row.edit_version == opened + 1 and row.saved_by_user_id == sam.id


def test_an_opening_save_with_a_version_on_a_day_with_no_report_starts_it(client, shop):
    """The report the page was read at is gone (deleted since): the save starts the day again."""
    resp = _put(client, shop, "opening", {"cash_addition": 30, "edit_version": 4})

    assert resp.status_code == 200, resp.content
    assert Report.objects.get(entity_id=shop.entity.id, transaction_date=D3).edit_version == 1


def test_a_save_without_a_version_is_applied_unchecked(client, shop, sam):
    """A page from before the check (the redesign Worker, an old bundle during a deploy)."""
    draft = make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, opening_balance=0,
                        cash_addition=30, current_section="sales", completed_sections=["opening"])
    _saved_by(draft, sam)

    resp = _put(client, shop, "opening", {"cash_addition": 40})

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(id=draft.id)
    assert row.cash_addition == Decimal("40.00") and row.edit_version == 2


@pytest.mark.parametrize("bad", ["1", -1, 1.5, True, [1]])
def test_a_version_that_is_not_a_whole_number_is_422(client, shop, bad):
    make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, opening_balance=0, cash_addition=0,
                current_section="opening", completed_sections=[])

    resp = _put(client, shop, "opening", {"cash_addition": 1, "edit_version": bad})

    assert resp.status_code == 422
    assert resp.json() == {"error": "This step's version doesn't look right. Reload the page, then save again."}


@pytest.mark.parametrize("step", ["sales", "expenses", "deposit"])
def test_a_null_version_is_422_where_the_report_always_exists(client, shop, step):
    _draft(shop)
    assert _put(client, shop, step, {"edit_version": None}).status_code == 422


# --- R2: Sales ----------------------------------------------------------------------------------------


@pytest.fixture
def methods(shop):
    rows = {}
    for name, value_name, sale_type, order in (("Cash", "cash_sales", SaleType.OTHER, 0),
                                               ("Visa", "visa_sales", SaleType.ELECTRONIC, 1)):
        row = SaleInfo.objects.create(sale_name=name, value_name=value_name, type=sale_type, display_order=order)
        EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=row.id, is_active=True, display_order=order)
        rows[value_name] = row
    return rows


def test_a_sales_save_from_an_older_version_is_refused_and_writes_nothing(client, shop, sam, methods):
    draft = _draft(shop, current_section="sales", completed_sections=["opening"], cashsale_total=0, expense_total=0)
    ReportSale.objects.create(report_id=draft.id, sale_id=methods["visa_sales"].id, amount=Decimal("80.00"))
    opened = _get(client, shop, "sales").json()["edit_version"]
    _saved_by(draft, sam)

    resp = _put(client, shop, "sales", {"cash_sales": 500, "amounts": {"visa_sales": 1}, "edit_version": opened})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == _sentence(draft, "Sam Second") and body["next"] == "expenses"
    assert body["saved"] == _get(client, shop, "sales").json()
    assert [m["amount"] for m in body["saved"]["methods"]] == [80.0]
    assert list(ReportSale.objects.filter(report_id=draft.id).values_list("amount", flat=True)) == [Decimal("80.00")]
    row = Report.objects.get(id=draft.id)
    assert row.cashsale_total == Decimal("0.00") and row.current_section == "sales" and _history(draft) == 0


def test_a_sales_save_on_the_current_version_applies(client, shop, sam, methods):
    draft = _draft(shop, current_section="sales", completed_sections=["opening"], cashsale_total=0, expense_total=0)
    opened = _get(client, shop, "sales").json()["edit_version"]

    resp = _put(client, shop, "sales", {"cash_sales": 50, "amounts": {"visa_sales": 10}, "edit_version": opened},
                user=sam)

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(id=draft.id)
    assert row.cashsale_total == Decimal("50.00") and row.edit_version == opened + 1 and row.saved_by_user_id == sam.id


# --- R2: Expenses - the step versioned, the lines additive ---------------------------------------------


class _Bucket:
    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, fileobj, bucket, key, **kwargs):
        self.objects[key] = fileobj.read()


@pytest.fixture
def lists(shop, monkeypatch):
    monkeypatch.setattr(storage, "client", lambda: _Bucket())
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-0001", name="ABC Supplies")
    account = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0001", xero_code="400",
                                         name="Advertising", type="EXPENSE", status="ACTIVE")
    EntityAccountXero.objects.create(account_id=account.id, name="Advertising", type="EXPENSE", is_active=True)


def _add_line(client, shop, user, amount):
    form = {"transaction_date": D3.isoformat(), "amount": amount, "description": "Flyers",
            "supplier_id": "con-0001", "account_id": "acc-0001",
            "receipt": SimpleUploadedFile("slip.png", PNG, content_type="image/png")}
    return client.post(url(shop.entity.id, "/reports/expenses/lines"), form, headers=headers(user.id))


def test_two_peoples_lines_both_stay_and_never_move_the_version(client, shop, sam, lists):
    draft = _draft(shop, current_section="expenses", completed_sections=["opening", "sales"], expense_total=0)
    opened = _get(client, shop, "expenses").json()["edit_version"]

    assert _add_line(client, shop, shop.owner, "10").status_code == 201
    assert _add_line(client, shop, sam, "20").status_code == 201

    assert ReportExpense.objects.filter(report_id=draft.id).count() == 2
    assert Report.objects.get(id=draft.id).edit_version == opened
    # the step's save, opened before both lines, still goes through
    resp = _put(client, shop, "expenses", {"edit_version": opened})
    assert resp.status_code == 200, resp.content
    assert Report.objects.get(id=draft.id).edit_version == opened + 1


def test_an_expenses_save_from_an_older_version_is_refused(client, shop, sam, lists):
    draft = _draft(shop, current_section="expenses", completed_sections=["opening", "sales"], expense_total=0)
    opened = _get(client, shop, "expenses").json()["edit_version"]
    _saved_by(draft, sam)

    resp = _put(client, shop, "expenses", {"edit_version": opened})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == _sentence(draft, "Sam Second") and body["next"] == "deposit"
    assert body["saved"] == _get(client, shop, "expenses").json()
    assert Report.objects.get(id=draft.id).completed_sections == ["opening", "sales"]


# --- R2: Bank Deposit and Cash Count ------------------------------------------------------------------


def test_a_deposit_save_from_an_older_version_is_refused_and_writes_nothing(client, shop, sam):
    draft = _draft(shop, bank_deposit=30)
    opened = _get(client, shop, "deposit").json()["edit_version"]
    _saved_by(draft, sam)

    resp = _put(client, shop, "deposit", {"bank_deposit": 100, "edit_version": opened})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == _sentence(draft, "Sam Second") and body["next"] == "cash_count"
    assert body["saved"] == _get(client, shop, "deposit").json() and body["saved"]["bank_deposit"] == 30.0
    row = Report.objects.get(id=draft.id)
    assert row.bank_deposit == Decimal("30.00") and row.current_section == "deposit" and _history(draft) == 0


def test_a_deposit_save_on_the_current_version_applies(client, shop, sam):
    draft = _draft(shop)
    opened = _get(client, shop, "deposit").json()["edit_version"]

    resp = _put(client, shop, "deposit", {"bank_deposit": 100, "edit_version": opened}, user=sam)

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(id=draft.id)
    assert row.bank_deposit == Decimal("100.00") and row.edit_version == opened + 1 and row.saved_by_user_id == sam.id


@pytest.mark.parametrize("with_deposit", [False, True])
def test_a_count_save_from_an_older_version_is_refused_and_writes_nothing(client, shop, sam, notes, with_deposit):
    draft = _draft(shop, bank_deposit=100, closing_balance=55, current_section="cash_count",
                   completed_sections=["opening", "sales", "expenses", "deposit"])
    opened = _get(client, shop, "cash-count").json()["edit_version"]
    _saved_by(draft, sam)
    body = {"counts": {notes[50]: 1, notes[5]: 1}, "edit_version": opened}
    if with_deposit:
        body["bank_deposit"] = 90

    resp = _put(client, shop, "cash-count", body)

    assert resp.status_code == 409
    answer = resp.json()
    assert answer["error"] == _sentence(draft, "Sam Second") and answer["next"] == "ending"
    assert answer["saved"] == _get(client, shop, "cash-count").json()
    assert not ReportCashCount.objects.filter(report_id=draft.id).exists()
    row = Report.objects.get(id=draft.id)
    assert row.bank_deposit == Decimal("100.00") and row.current_section == "cash_count" and _history(draft) == 0


def test_a_count_save_on_the_current_version_applies(client, shop, sam, notes):
    draft = _draft(shop, bank_deposit=100, closing_balance=55, current_section="cash_count",
                   completed_sections=["opening", "sales", "expenses", "deposit"])
    opened = _get(client, shop, "cash-count").json()["edit_version"]

    resp = _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 1}, "edit_version": opened}, user=sam)

    assert resp.status_code == 200, resp.content
    row = Report.objects.get(id=draft.id)
    assert row.current_section == "ending" and row.edit_version == opened + 1 and row.saved_by_user_id == sam.id


def test_the_saver_with_no_name_is_named_by_email(client, shop):
    nameless = make_user("nameless@test.com", first_name="", last_name="")
    UserEntity.objects.create(user_id=nameless.id, entity_id=shop.entity.id, role=EntityRole.ADMIN, approved=True)
    draft = _draft(shop)
    opened = _get(client, shop, "deposit").json()["edit_version"]
    _saved_by(draft, nameless)

    resp = _put(client, shop, "deposit", {"bank_deposit": 1, "edit_version": opened})

    assert resp.status_code == 409 and resp.json()["error"] == _sentence(draft, "nameless@test.com")
