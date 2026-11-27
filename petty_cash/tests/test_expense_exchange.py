# ruff: noqa: F811 - pytest fixtures imported from the sibling suites are used as parameters
"""A receipt in another currency (owner, 2026-11-19): the reader converts its amount at
Frankfurter's rate (``minty_shared.fx``; the rule's own cases are minty-shared-py's
``tests/test_fx.py``), and a saved line keeps how it was converted - all six details or none, and
only while its amount is the converted one. No network: the rate is a stand-in."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.models import ReportExpense
from minty_shared import fx

from petty_cash.tests.conftest import headers, url
from petty_cash.tests.test_expense_extract import FakeResponse, _reply, model  # noqa: F401 - the model fixture
from petty_cash.tests.test_expenses import D3, PNG, _add, _draft, _line, lists, s3  # noqa: F401 - fixtures

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("lists", "on_trial")]

RECEIPT_DAY = D3 - timedelta(days=2)


@pytest.fixture
def rates(monkeypatch):
    """Frankfurter, stood in: 7.846 HKD a USD for whatever day is asked, answered as that day."""
    asked: list[tuple[str, str, date]] = []
    answer = {"rate": fx.Rate(rate=Decimal("7.846"), date=RECEIPT_DAY)}

    def fake(base, quote, on):
        asked.append((base, quote, on))
        return answer["rate"]

    monkeypatch.setattr(fx, "frankfurter_rate", fake)
    return {"asked": asked, "answer": answer}


def _extract(client, shop, model, *, day=D3, **reply):
    model.answer = FakeResponse(_reply(**reply))
    form = {"receipt": SimpleUploadedFile("slip.jpg", PNG, content_type="image/jpeg")}
    if day is not None:
        form["transaction_date"] = day.isoformat()
    return client.post(url(shop.entity.id, "/reports/expenses/extract"), form, headers=headers(shop.owner.id))


# --- the read --------------------------------------------------------------------------------------


def test_a_receipt_in_another_currency_comes_back_converted_and_says_how(client, shop, model, rates):
    body = _extract(client, shop, model, currency="USD", amount=12.34,
                    date=RECEIPT_DAY.isoformat()).json()

    assert rates["asked"] == [("USD", "HKD", RECEIPT_DAY)]
    s = body["suggestions"]
    assert s["amount"]["value"] == "96.82" and s["amount"]["applied"] is True
    assert s["exchange"] == {
        "original_currency": "USD", "original_amount": "12.34", "rate": "7.846",
        "rate_date": RECEIPT_DAY.isoformat(), "date_basis": "receipt", "source": "Frankfurter",
        "asked_date": RECEIPT_DAY.isoformat(),
    }


def test_a_refused_read_asks_for_no_rate(client, shop, model, rates):
    # several receipts in the file (the only refusal, the user 2026-11-25): nothing is filled,
    # so no rate
    body = _extract(client, shop, model, currency="USD", amount=12.34, document_count=2).json()
    assert rates["asked"] == [] and body["suggestions"] is None


def test_a_read_with_no_description_is_still_converted(client, shop, model, rates):
    # the user, 2026-11-25: the description is not mandatory, so the amount fills and converts
    body = _extract(client, shop, model, currency="USD", amount=12.34, description="").json()
    assert [(f, t) for f, t, _ in rates["asked"]] == [("USD", "HKD")]
    assert body["suggestions"]["amount"]["applied"] is True


def test_the_company_s_own_currency_asks_for_no_rate(client, shop, model, rates):
    s = _extract(client, shop, model, currency="HKD", amount=120).json()["suggestions"]
    assert rates["asked"] == [] and s["amount"]["value"] == "120.00" and "exchange" not in s


def test_no_rate_leaves_the_amount_empty_and_names_the_currency(client, shop, model, rates):
    rates["answer"]["rate"] = None
    s = _extract(client, shop, model, currency="TWD", amount=300).json()["suggestions"]
    assert s["amount"]["value"] == "" and s["amount"]["applied"] is False
    assert s["exchange_failed"] == {"currency": "TWD"} and "exchange" not in s


def test_a_receipt_with_no_date_uses_the_report_s_day(client, shop, model, rates):
    _extract(client, shop, model, currency="USD", amount=12.34, date="")
    assert rates["asked"] == [("USD", "HKD", D3)]


def test_without_the_report_s_day_the_company_s_today_is_used(client, shop, model, rates):
    # an older web sends no transaction_date
    _extract(client, shop, model, day=None, currency="USD", amount=12.34, date="")
    assert len(rates["asked"]) == 1 and rates["asked"][0][2] >= D3


def test_a_day_that_is_not_a_date_is_refused(client, shop, model, rates):
    model.answer = FakeResponse(_reply())
    resp = client.post(url(shop.entity.id, "/reports/expenses/extract"),
                       {"receipt": SimpleUploadedFile("slip.jpg", PNG, content_type="image/jpeg"),
                        "transaction_date": "yesterday"}, headers=headers(shop.owner.id))
    assert resp.status_code == 422


def test_the_cap_is_on_the_converted_amount(client, shop, model, rates):
    # 20,000,000 KRW is over the 10,000,000 cap as read, but about HKD 112,000 converted
    rates["answer"]["rate"] = fx.Rate(rate=Decimal("0.0056"), date=RECEIPT_DAY)
    s = _extract(client, shop, model, currency="KRW", amount=20_000_000).json()["suggestions"]
    assert s["amount"]["value"] == "112000.00"


# --- the saved line --------------------------------------------------------------------------------

SIX = {
    "original_currency": "USD", "original_amount": "12.34", "exchange_rate": "7.846",
    "exchange_rate_date": RECEIPT_DAY.isoformat(), "exchange_rate_basis": "receipt",
    "exchange_rate_source": "Frankfurter",
}


def _kept(line_id):
    row = ReportExpense.objects.get(id=line_id)
    return {name: getattr(row, name) for name in fx.COLUMNS}


def test_a_line_keeps_how_its_amount_was_converted(client, shop, s3):
    draft = _draft(shop)
    resp = _add(client, shop, amount="96.82", **SIX)
    assert resp.status_code == 201, resp.content
    line = ReportExpense.objects.get(report_id=draft.id)
    assert _kept(line.id) == {
        "original_currency": "USD", "original_amount": Decimal("12.34"), "exchange_rate": Decimal("7.846"),
        "exchange_rate_date": RECEIPT_DAY, "exchange_rate_basis": "receipt", "exchange_rate_source": "Frankfurter",
    }


def test_a_line_without_them_keeps_none(client, shop, s3):
    draft = _draft(shop)
    assert _add(client, shop).status_code == 201
    line = ReportExpense.objects.get(report_id=draft.id)
    assert set(_kept(line.id).values()) == {None}


@pytest.mark.parametrize("change", [{"amount": "100.00"}, {"exchange_rate_source": "ECB"},
                                    {"exchange_rate": ""}])
def test_details_that_do_not_hold_together_are_refused_and_nothing_is_saved(client, shop, s3, change):
    draft = _draft(shop)
    resp = _add(client, shop, **{"amount": "96.82", **SIX, **change})
    assert resp.status_code == 422
    assert resp.json()["error"] == fx.EXCHANGE_REFUSED
    assert not ReportExpense.objects.filter(report_id=draft.id).exists()


def _edit(client, shop, line, **form):
    from django.test.client import encode_multipart

    body = encode_multipart("BoUnDaRy", form)
    return client.patch(url(shop.entity.id, f"/expense-lines/{line.id}"), body,
                        content_type="multipart/form-data; boundary=BoUnDaRy", headers=headers(shop.owner.id))


def test_changing_the_amount_drops_the_conversion_and_a_new_one_replaces_it(client, shop, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3, amount="96.82")
    ReportExpense.objects.filter(id=line.id).update(**fx.saved_exchange(
        SIX, amount=Decimal("96.82"), company_currency="HKD", places=2, today=date.today()))

    assert _edit(client, shop, line, description="Taxi").status_code == 200
    assert _kept(line.id)["original_currency"] == "USD"  # the amount didn't change: it stays

    assert _edit(client, shop, line, amount="100").status_code == 200
    assert set(_kept(line.id).values()) == {None}

    assert _edit(client, shop, line, amount="96.82", **SIX).status_code == 200
    assert _kept(line.id)["exchange_rate"] == Decimal("7.846")
