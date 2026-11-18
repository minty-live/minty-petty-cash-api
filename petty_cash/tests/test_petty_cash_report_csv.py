"""History's Petty Cash Report CSV (``GET /reports/petty-cash-report.csv``,
``services/petty_cash_report_csv.py``; owner, 2026-11-13): each submitted day's report, then its
sales (one line a method, positive) and expense lines (negative) with their account and supplier."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from minty_db.enums import EntityRole, SaleType
from minty_db.models import (
    AccountInfo,
    Entity,
    EntitySaleSetting,
    ReportExpense,
    ReportSale,
    SaleInfo,
    UserEntity,
    XeroContactSync,
)

from petty_cash.tests.conftest import WEB, headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3

pytestmark = pytest.mark.django_db

PATH = "/reports/petty-cash-report.csv"
HEADER = ["Date", "Line", "Status", "Account Code", "Account Name", "Contact", "Description", "Amount",
          "Opening Balance", "Cash Added", "Cash Sales", "Non-cash Sales", "Expenses", "Bank Deposit",
          "Cash Short/Over", "Closing Balance", "Submitted By", "Currency"]
NO_FIGURES = [""] * 9  # Opening Balance .. Submitted By


def _day(days_back):
    return (D3 - timedelta(days=days_back)).isoformat()


def _range(days_back_start=2, days_back_end=0):
    return {"start_date": _day(days_back_start), "end_date": _day(days_back_end)}


@pytest.fixture
def books(shop):
    """Two submitted days (the first published) and a draft before them. The lines: one with its
    account and supplier, one whose account and supplier were deleted (the keys emptied; the
    published names stand in) and typed nothing, one on an account with a Minty code only and no
    supplier. The sales: Cash Sales, Octopus and Foodpanda in the company's order; the second day
    has a zero (left out), a method since removed that still holds an amount (after the others),
    and its cash only in the report's cash column (as older reports keep it)."""
    entity = shop.entity
    tape = AccountInfo.objects.create(id=uuid.uuid4(), entity_id=entity.id, type="EXPENSE", name="Tape",
                                      xero_account_id=str(uuid.uuid4()), xero_code="445", status="ACTIVE")
    tea = AccountInfo.objects.create(id=uuid.uuid4(), entity_id=entity.id, type="EXPENSE", name="Tea",
                                     code="460", status="ACTIVE")
    abc = XeroContactSync.objects.create(id=uuid.uuid4(), entity_id=entity.id, name="ABC Supplies")
    base = datetime(2026, 10, 1, 9, tzinfo=UTC)
    methods = {}
    for order, (name, value_name, kind) in enumerate([("Foodpanda", "foodpanda_sales", SaleType.DELIVERY),
                                                       ("Cash Sales", "cash_sales", SaleType.OTHER),
                                                       ("Octopus", "octopus_sales", SaleType.ELECTRONIC)]):
        row = SaleInfo.objects.filter(value_name=value_name).first() or SaleInfo.objects.create(
            sale_name=name, value_name=value_name, type=kind)
        # the company's order is the link's, not the catalogue's: Cash Sales, Octopus, Foodpanda
        EntitySaleSetting.objects.create(entity_id=entity.id, sale_id=row.id, is_active=True,
                                         display_order={"cash_sales": 1, "octopus_sales": 2}.get(value_name, 3))
        methods[value_name] = row
    gone = SaleInfo.objects.create(sale_name="Old Wallet", value_name="oldwallet_sales", type=SaleType.ELECTRONIC)

    def sale(row, method, amount):
        ReportSale.objects.create(report_id=row.id, sale_id=method.id, amount=Decimal(str(amount)))

    def report(days_back, status="submitted", **fields):
        return make_report(entity, D3 - timedelta(days=days_back), status=status, **fields)

    def line(row, minutes, item, amount, description, **fields):
        ReportExpense.objects.create(id=uuid.uuid4(), report_id=row.id, item=item, amount=Decimal(str(amount)),
                                     description=description, created_at=base + timedelta(minutes=minutes),
                                     **fields)

    draft = report(2, status="draft", created_by=shop.owner.id, opening_balance=0, closing_balance=0)
    line(draft, 0, "Draft line", 7, "Draft")
    first = report(1, created_by=shop.owner.id, xero_integrated=True, opening_balance=100, cash_addition=20,
                   cashsale_total=300, nocashsale_total=40, expense_total=Decimal("35.1"), bank_deposit=500,
                   discrepancy_amount=Decimal("2.5"), discrepancy_type="short", closing_balance=Decimal("47.4"))
    line(first, 2, "Blank", 10, "No description", account_id=None, contact_id=None,
         published_account_code="400", published_account_name="Advertising",
         published_contact_name="Old Supplier")
    line(first, 1, "Tape", "25.10", "Tape, the wide roll", account_id=tape.id, contact_id=abc.id)
    sale(first, methods["foodpanda_sales"], 15)
    sale(first, methods["cash_sales"], 300)
    sale(first, methods["octopus_sales"], 25)
    second = report(0, opening_balance=Decimal("47.4"), cashsale_total=60, nocashsale_total=8,
                    expense_total=Decimal("12.5"), discrepancy_amount=1, discrepancy_type="over",
                    closing_balance=Decimal("95.9"))
    line(second, 0, "Tea", "12.5", "Café 茶", account_id=tea.id)
    sale(second, methods["octopus_sales"], 0)
    sale(second, gone, 8)
    return shop


ROWS = [
    [_day(1), "Daily report", "Published to Xero", "", "", "", "", "",
     "100.00", "20.00", "300.00", "40.00", "35.10", "500.00", "-2.50", "47.40", "Olive", "HKD"],
    [_day(1), "Sale", "", "", "", "", "Cash Sales", "300.00", *NO_FIGURES, "HKD"],
    [_day(1), "Sale", "", "", "", "", "Octopus", "25.00", *NO_FIGURES, "HKD"],
    [_day(1), "Sale", "", "", "", "", "Foodpanda", "15.00", *NO_FIGURES, "HKD"],
    [_day(1), "Expense", "", "445", "Tape", "ABC Supplies", "Tape, the wide roll", "-25.10", *NO_FIGURES, "HKD"],
    [_day(1), "Expense", "", "400", "Advertising", "Old Supplier", "Blank", "-10.00", *NO_FIGURES, "HKD"],
    [_day(0), "Daily report", "Submitted", "", "", "", "", "",
     "47.40", "0.00", "60.00", "8.00", "12.50", "0.00", "1.00", "95.90", "", "HKD"],
    [_day(0), "Sale", "", "", "", "", "Cash Sales", "60.00", *NO_FIGURES, "HKD"],
    [_day(0), "Sale", "", "", "", "", "Old Wallet", "8.00", *NO_FIGURES, "HKD"],
    [_day(0), "Expense", "", "460", "Tea", "", "Café 茶", "-12.50", *NO_FIGURES, "HKD"],
]


def _get(client, entity, user, **query):
    return client.get(url(entity.id, PATH), query, headers=headers(user.id))


def _table(resp):
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))


def test_each_day_is_its_report_then_its_sales_then_its_expenses(client, books):
    resp = _get(client, books.entity, books.owner, **_range(2, 0))

    assert resp.status_code == 200, resp.content[:300]
    assert _table(resp) == [HEADER] + ROWS  # the draft's day is left out
    assert resp.content.endswith(b"\n") and not resp.content.startswith(b"\xef\xbb\xbf")
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    assert resp["Content-Disposition"] == (
        f"attachment; filename=acme-shop_petty-cash-report_{_day(2)}_to_{_day(0)}.csv")
    assert resp["Access-Control-Allow-Origin"] == WEB
    assert resp["Access-Control-Expose-Headers"] == "Content-Disposition"


def test_the_queries_do_not_grow_with_the_days(client, books):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count():
        with CaptureQueriesContext(connection) as seen:
            assert _get(client, books.entity, books.owner).status_code == 200
        return len(seen)

    before = count()
    for n in range(2, 6):  # four more submitted days, each with a sale and an expense
        day = make_report(books.entity, D3 + timedelta(days=n), opening_balance=0, closing_balance=0)
        ReportSale.objects.create(report_id=day.id, sale_id=SaleInfo.objects.get(value_name="octopus_sales").id,
                                  amount=1)
        ReportExpense.objects.create(id=uuid.uuid4(), report_id=day.id, item="Tea", amount=1, description="x")
    assert count() == before


def test_a_company_with_no_currency_says_so(client, books):
    Entity.objects.filter(id=books.entity.id).update(currency_id=None)

    rows = _table(_get(client, books.entity, books.owner, **_range(0, 0)))[1:]

    assert {r[-1] for r in rows} == {"No currency selected"}


def test_the_movements_export_is_unchanged_beside_it(client, books):
    resp = client.get(url(books.entity.id, "/reports/export.csv"), _range(1, 1), headers=headers(books.owner.id))
    assert resp.status_code == 200
    assert _table(resp)[0] == ["Date", "Account Code", "Amount", "Description", "Reference", "Check Number"]
    assert resp["Content-Disposition"] == f"attachment; filename=acme-shop_reports_{_day(1)}_to_{_day(1)}.csv"


def test_with_no_dates_it_is_the_whole_history(client, books):
    resp = _get(client, books.entity, books.owner)

    assert resp.status_code == 200, resp.content[:300]
    assert _table(resp) == [HEADER] + ROWS  # every submitted day; the draft still left out
    assert resp["Content-Disposition"] == (
        f"attachment; filename=acme-shop_petty-cash-report_{_day(1)}_to_{_day(0)}.csv")


def test_with_no_dates_and_nothing_submitted_is_404(client, shop):
    make_report(shop.entity, D3, status="draft", opening_balance=0, closing_balance=0)
    resp = _get(client, shop.entity, shop.owner)
    assert resp.status_code == 404 and resp.json() == {"error": "There are no submitted reports to export yet."}


@pytest.mark.parametrize("query, message", [
    ({"start_date": "2026-10-01"}, "Choose both dates to export the reports between them."),
    ({"end_date": "2026-10-01"}, "Choose both dates to export the reports between them."),
    ({"start_date": "01/10/2026", "end_date": "2026-10-02"},
     "A date isn't in the YYYY-MM-DD form. Choose it from the calendar."),
    ({"start_date": "2026-10-05", "end_date": "2026-10-02"},
     "The start date is after the end date. Choose a start on or before the end."),
    ({"start_date": "2025-01-01", "end_date": "2026-01-02"},
     "The range is longer than a year. Choose 366 days or fewer."),
])
def test_a_bad_range_is_refused(client, books, query, message):
    resp = _get(client, books.entity, books.owner, **query)
    assert resp.status_code == 422 and resp.json() == {"error": message}


def test_nothing_to_export_is_404(client, books):
    for query in ({"start_date": "2020-01-01", "end_date": "2020-01-31"}, _range(2, 2)):  # nothing; a draft only
        resp = _get(client, books.entity, books.owner, **query)
        assert resp.status_code == 404 and resp.json() == {
            "error": "There are no submitted reports between those dates. Choose another range."}


def test_it_needs_the_company_wide_view(client, books):
    base = make_user("base@test.com", first_name="Bea")
    UserEntity.objects.create(user_id=base.id, entity_id=books.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    resp = _get(client, books.entity, base, **_range())
    assert resp.status_code == 403 and resp.json() == {
        "error": "You don't have permission to export this company's reports."}

    cashier = make_user("cashier@test.com", first_name="Cass")
    UserEntity.objects.create(user_id=cashier.id, entity_id=books.entity.id, role=EntityRole.CASHIER, approved=True)
    assert _get(client, books.entity, cashier, **_range()).status_code == 200


def test_another_companys_export_is_refused_at_the_door(client, books):
    stranger = make_user("stranger@test.com", first_name="Sam")
    theirs = make_entity(stranger, name="Their Shop")
    assert _get(client, theirs, books.owner, **_range()).status_code == 403
