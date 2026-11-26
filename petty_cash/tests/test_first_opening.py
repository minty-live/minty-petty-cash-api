"""The company's starting cash, asked at its FIRST report (the user, 2026-11-26).

Onboarding may skip the opening balance ("Skip for now"): it then stays NOT SET - ``start_date`` and
``opening_balance`` NULL, never 0 - and the first report may not start until someone gives it:
``PUT /opening-balance {start_date, opening_balance}`` saves both on ``entity_pettycash_settings``
(``REPORT_EDIT_OWN``, the same as starting a report), then the app goes into that day's Opening step,
which opens at the amount. Two people at once: the first saves; the second is 409 ``OPENING_SET``
with what was saved, and nothing of theirs is written. ``GET /home`` says ``opening_set``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from minty_db.enums import EntityRole
from minty_db.models import CurrencyInfo, Entity, EntityPettycashSettings, Report, UserEntity

from petty_cash.days import today
from petty_cash.tests.conftest import headers, make_report, make_user, url

pytestmark = pytest.mark.django_db
TODAY = today()
D2, D3 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)


def _set(client, shop, body, user=None):
    return client.put(url(shop.entity.id, "/opening-balance"), body, content_type="application/json",
                      headers=headers((user or shop.owner).id))


def _settings(shop):
    return EntityPettycashSettings.objects.filter(entity_id=shop.entity.id).first()


def _home(client, shop):
    resp = client.get(url(shop.entity.id, "/home"), headers=headers(shop.owner.id))
    assert resp.status_code == 200, resp.content
    return resp.json()


def _start_report(client, shop, day):
    body = {"transaction_date": day.isoformat(), "cash_addition": 0, "withdrawal": "personal", "action": "next"}
    return client.put(url(shop.entity.id, "/reports/opening"), body, content_type="application/json",
                      headers=headers(shop.owner.id))


# --- setting it -------------------------------------------------------------------------------------


def test_the_starting_cash_is_saved_and_the_first_report_opens_at_it(client, shop):
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, opening_skipped_at=timezone.now())

    resp = _set(client, shop, {"start_date": D2.isoformat(), "opening_balance": 350.5})

    assert resp.status_code == 200, resp.content
    assert resp.json() == {"start_date": D2.isoformat(), "opening_balance": 350.5}
    row = _settings(shop)
    assert (row.start_date, row.opening_balance, row.opening_skipped_at) == (D2, Decimal("350.50"), None)
    opening = client.get(url(shop.entity.id, "/reports/opening"), {"transaction_date": D2.isoformat()},
                         headers=headers(shop.owner.id)).json()
    assert opening["starting_cash_balance"] == 350.5
    assert _start_report(client, shop, D2).status_code == 200
    report = Report.objects.get(entity_id=shop.entity.id, transaction_date=D2)
    assert report.opening_balance == Decimal("350.50")


def test_zero_is_a_starting_cash_not_a_missing_one(client, shop):
    assert _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 0}).status_code == 200
    row = _settings(shop)
    assert row.opening_balance == Decimal("0.00") and row.start_date == TODAY
    assert _home(client, shop)["opening_set"] is True


def test_a_cashier_who_can_start_a_report_may_set_it(client, shop):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)

    resp = _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 10}, user=cashier)

    assert resp.status_code == 200, resp.content


def test_someone_outside_the_company_cannot(client, shop):
    stranger = make_user("stranger@test.com")
    resp = _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 10}, user=stranger)
    assert resp.status_code in (403, 404)
    assert _settings(shop) is None


# --- two people, or too late -------------------------------------------------------------------------


def test_the_second_person_gets_what_the_first_saved_and_overwrites_nothing(client, shop):
    assert _set(client, shop, {"start_date": D2.isoformat(), "opening_balance": 100}).status_code == 200

    resp = _set(client, shop, {"start_date": D3.isoformat(), "opening_balance": 999})

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "The starting cash was just set by someone else.",
        "code": "OPENING_SET",
        "start_date": D2.isoformat(),
        "opening_balance": 100.0,
    }
    row = _settings(shop)
    assert (row.start_date, row.opening_balance) == (D2, Decimal("100.00"))


def test_a_save_whose_check_ran_before_the_other_landed_is_still_turned_away(client, shop, monkeypatch):
    """The decision is made on the LOCKED row, inside the transaction - not on an earlier read."""
    from petty_cash.services import opening as service

    assert _set(client, shop, {"start_date": D2.isoformat(), "opening_balance": 100}).status_code == 200
    monkeypatch.setattr(service, "opening_not_set", lambda entity_id: True)  # a stale "not set" read

    resp = _set(client, shop, {"start_date": D3.isoformat(), "opening_balance": 999})

    assert resp.status_code == 409 and resp.json()["code"] == "OPENING_SET"
    assert _settings(shop).opening_balance == Decimal("100.00")


def test_once_a_report_exists_it_is_too_late(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=50)

    resp = _set(client, shop, {"start_date": D3.isoformat(), "opening_balance": 10})

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "Your first report has already started, so the starting cash can't be set here.",
        "code": "REPORT_EXISTS",
        "to": "dashboard",
    }
    assert _settings(shop) is None or _settings(shop).start_date is None


# --- refused input -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"opening_balance": 10}, "Choose the start date to continue."),
        ({"start_date": "26/11/2026", "opening_balance": 10}, "Choose the start date to continue."),
        ({"start_date": "TOMORROW", "opening_balance": 10}, "Choose the start date to continue."),
        ({"start_date": "TODAY"}, "The cash in your petty cash box is still empty. Add the amount to continue."),
        ({"start_date": "TODAY", "opening_balance": None},
         "The cash in your petty cash box is still empty. Add the amount to continue."),
        ({"start_date": "TODAY", "opening_balance": -1}, "Enter the cash in your petty cash box as 0 or more."),
        ({"start_date": "TODAY", "opening_balance": "12"}, "Enter the cash in your petty cash box as 0 or more."),
        ({"start_date": "TODAY", "opening_balance": 1.005},
         "That amount has more decimal places than HKD allows (2)."),
    ],
)
def test_bad_input_is_refused_and_nothing_is_saved(client, shop, body, error):
    body = dict(body)
    if body.get("start_date") == "TODAY":
        body["start_date"] = TODAY.isoformat()
    resp = _set(client, shop, body)
    assert resp.status_code == 422, resp.content
    assert resp.json()["error"] == error
    assert _settings(shop) is None


def test_a_future_start_date_is_refused(client, shop):
    resp = _set(client, shop, {"start_date": (TODAY + timedelta(days=1)).isoformat(), "opening_balance": 10})
    assert resp.status_code == 422
    assert resp.json()["error"].startswith("Transaction date cannot be in the future.")


def test_whole_amounts_only_in_a_currency_without_decimals(client, shop):
    jpy = CurrencyInfo.objects.create(currency_code="JPY", currency_name="Yen", symbol="¥", decimal_places=0,
                                      is_active=True)
    Entity.objects.filter(id=shop.entity.id).update(currency_id=jpy.id)

    resp = _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 100.5})

    assert resp.status_code == 422
    assert resp.json()["error"] == "That amount has more decimal places than JPY allows (0)."
    assert _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 100}).status_code == 200


def test_a_company_with_no_currency_picks_one_first(client, shop):
    Entity.objects.filter(id=shop.entity.id).update(currency_id=None)
    resp = _set(client, shop, {"start_date": TODAY.isoformat(), "opening_balance": 10})
    assert resp.status_code == 409 and resp.json()["to"] == "dashboard"


# --- what the dashboard and the Opening step see -----------------------------------------------------


def test_home_says_whether_the_starting_cash_is_set(client, shop):
    assert _home(client, shop)["opening_set"] is False  # no settings row
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id)
    assert _home(client, shop)["opening_set"] is False  # a row with nothing in it
    EntityPettycashSettings.objects.filter(entity_id=shop.entity.id).update(opening_balance=0, start_date=D2)
    assert _home(client, shop)["opening_set"] is True


def test_no_first_report_starts_while_the_starting_cash_is_not_set(client, shop):
    resp = _start_report(client, shop, D3)

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "Set your starting cash before your first report.",
        "code": "OPENING_NOT_SET",
        "to": "dashboard",
    }
    assert not Report.objects.filter(entity_id=shop.entity.id).exists()


def test_a_company_already_reporting_is_never_asked(client, shop):
    """Older companies have reports and no start date: the rule is for the FIRST report only."""
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=80)
    assert _home(client, shop)["opening_set"] is False
    assert _start_report(client, shop, D3).status_code == 200
