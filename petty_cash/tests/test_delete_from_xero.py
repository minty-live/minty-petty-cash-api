# ruff: noqa: F811  (the publish tests' pytest fixtures, used by name as parameters)
"""Deleting a report that is in Xero (owner, 2026-11-17; ``services/unpublish.py``): its entries come
out of Xero first, and if Xero refuses any of them the delete is BLOCKED - the report stays, with a
list of what to sort out in Xero. Only an accountant and up deletes a report that has been to Xero.
A report with no usable record of its entries (loaded from the old system, or another organisation)
is deleted as before, the app having warned that its entries stay in Xero.

Xero is the publish tests' fake (``test_publish.FakeXero``), the token service theirs too.
"""

from __future__ import annotations

import pytest
from minty_db.enums import EntityRole
from minty_db.models import Report, UserEntity

from core.xero_tokens import XeroTokenUnavailable
from petty_cash.services import publish as pub
from petty_cash.services.reports import currency_code
from petty_cash.tests.conftest import headers, make_user, url
from petty_cash.tests.test_close import D3
from petty_cash.tests.test_publish import (  # noqa: F401  (fixtures)
    ORG,
    _full_day,
    _report,
    _Response,
    _run,
    mapped,
    xero,
)
from petty_cash.xero import publish_record

pytestmark = pytest.mark.django_db

KEPT = ("Xero didn't let me remove this report's entries, so I've kept the report. "
        "Sort these out in Xero, then delete it again.")


@pytest.fixture
def org(xero):
    """The organisation, with no lock date."""
    xero.answers[("GET", "Organisation")] = _Response(200, {"Organisations": [{"Name": "Acme"}]})
    return xero


def _delete(client, shop, report_id, user=None):
    return client.delete(url(shop.entity.id, f"/reports/{report_id}"), headers=headers((user or shop.owner).id))


def _published(shop):
    """The publish tests' full day, published: every kind of entry recorded."""
    report, first, second = _full_day(shop)
    _run(shop, report)
    assert report.publishing_status == "completed"
    return report, first, second, publish_record.load_record(report.id)["objects"]


def _writes(xero):
    return [(m, p) for m, p, _ in xero.calls if m in ("POST", "PUT", "DELETE")]


def _money(shop, text):
    return f"{currency_code(shop.entity.id)} {text}"


def _member(shop, email, role):
    person = make_user(email)
    UserEntity.objects.create(user_id=person.id, entity_id=shop.entity.id, role=role, approved=True)
    return person


def test_a_published_report_s_entries_come_out_of_xero_then_the_report_is_deleted(client, mapped, org):
    report, first, second, objects = _published(mapped)
    org.calls.clear()

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 200, resp.json()
    assert not Report.objects.filter(id=report.id).exists()
    deleted = {"status": "DELETED"}
    for xero_id in (objects["expenses"][str(first.id)]["id"], objects["expenses"][str(second.id)]["id"],
                    objects["discrepancy"]["id"]):
        assert org.sent("POST", f"BankTransactions/{xero_id}") == [
            {"bankTransactions": [{"bankTransactionID": xero_id, **deleted}]}]
    for module in ("deposit", "withdrawal_from"):  # a company withdrawal is a transfer
        assert org.sent("POST", f"BankTransfers/{objects[module]['id']}") == [{"Status": "DELETED"}]
    invoice = objects["invoices"]["id"]
    assert org.sent("POST", f"Invoices/{invoice}") == [{"Invoices": [{"InvoiceID": invoice, "Status": "VOIDED"}]}]


def test_a_reconciled_entry_blocks_the_delete_and_nothing_is_touched(client, mapped, org):
    report, first, _, objects = _published(mapped)
    coffee = objects["expenses"][str(first.id)]["id"]
    org.answers[("GET", f"BankTransactions/{coffee}")] = _Response(
        200, {"BankTransactions": [{"BankTransactionID": coffee, "IsReconciled": True, "Status": "AUTHORISED"}]})
    org.answers[("GET", f"Invoices/{objects['invoices']['id']}")] = _Response(
        200, {"Invoices": [{"InvoiceID": objects["invoices"]["id"], "Status": "PAID", "AmountPaid": 200}]})
    org.calls.clear()

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": KEPT, "reasons": [
        "Cash sales invoice: it has a payment in Xero. Remove the payment in Xero first.",
        f"Expense Coffee, {_money(mapped, '10.00')}: it's reconciled in Xero. Unreconcile it in Xero first.",
    ]}
    assert _writes(org) == []
    row = Report.objects.get(id=report.id)
    assert (row.publishing_status, row.xero_integrated, row.status) == ("completed", True, "published")
    assert publish_record.load_record(report.id)["objects"] == objects


def test_a_reconciled_transfer_blocks_the_delete(client, mapped, org):
    report, _, _, objects = _published(mapped)
    deposit = objects["deposit"]["id"]
    org.answers[("GET", f"BankTransfers/{deposit}")] = _Response(
        200, {"BankTransfers": [{"BankTransferID": deposit, "FromIsReconciled": False, "ToIsReconciled": True}]})

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json()["reasons"] == ["Bank deposit: it's reconciled in Xero. Unreconcile it in Xero first."]
    assert Report.objects.filter(id=report.id).exists()


def test_a_day_on_or_before_xero_s_lock_date_blocks_the_delete(client, mapped, org):
    report, *_ = _published(mapped)
    from datetime import UTC, datetime
    lock = int(datetime(D3.year, D3.month, D3.day, tzinfo=UTC).timestamp() * 1000)
    org.answers[("GET", "Organisation")] = _Response(
        200, {"Organisations": [{"PeriodLockDate": f"/Date({lock}+0000)/"}]})
    org.calls.clear()

    resp = _delete(client, mapped, report.id)

    day = f"{D3.day} {D3:%b %Y}"
    assert resp.status_code == 409
    assert resp.json()["reasons"] == [
        f"This day is on or before Xero's lock date ({day}). Move the lock date in Xero first."]
    assert _writes(org) == []


def test_an_entry_already_gone_from_xero_counts_as_removed(client, mapped, org):
    report, first, _, objects = _published(mapped)
    coffee = objects["expenses"][str(first.id)]["id"]
    invoice = objects["invoices"]["id"]
    org.answers[("GET", f"BankTransactions/{coffee}")] = _Response(404, {})
    org.answers[("GET", f"Invoices/{invoice}")] = _Response(
        200, {"Invoices": [{"InvoiceID": invoice, "Status": "VOIDED"}]})
    org.calls.clear()

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 200, resp.json()
    assert org.sent("POST", f"BankTransactions/{coffee}") == []
    assert org.sent("POST", f"Invoices/{invoice}") == []
    assert not Report.objects.filter(id=report.id).exists()


def test_a_removal_refused_part_way_keeps_the_report_out_of_xero(client, mapped, org):
    """The check passed, then Xero refused one removal: what came out is forgotten, the rest stays
    recorded, and the report is no longer marked in Xero, so a republish makes again what is gone."""
    report, first, second, objects = _published(mapped)
    deposit = objects["deposit"]["id"]
    org.answers[("POST", f"BankTransfers/{deposit}")] = _Response(
        400, {"Elements": [{"ValidationErrors": [{"Message": "This bank transfer cannot be deleted"}]}]})

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == KEPT and len(body["reasons"]) == 1
    assert body["reasons"][0].startswith("Bank deposit: ")
    row = Report.objects.get(id=report.id)
    assert (row.xero_integrated, row.status, row.publishing_status) == (False, "submitted", "completed")
    left = publish_record.load_record(report.id)["objects"]
    # in the publish's order: the withdrawal, the invoice and the expenses came out before the deposit
    assert set(left) == {"deposit", "discrepancy"}


def test_the_report_is_claimed_while_its_entries_come_out(client, mapped, org, monkeypatch):
    report, first, _, objects = _published(mapped)
    coffee = objects["expenses"][str(first.id)]["id"]
    org.answers[("GET", f"BankTransactions/{coffee}")] = _Response(
        200, {"BankTransactions": [{"BankTransactionID": coffee, "IsReconciled": True}]})
    seen = []
    real = org.request

    def spy(method, address, body):
        seen.append(Report.objects.get(id=report.id).publishing_status)
        return real(method, address, body)

    monkeypatch.setattr(org, "request", spy)

    assert _delete(client, mapped, report.id).status_code == 409

    assert seen and set(seen) == {"publishing"}  # publish, edits and a second delete are refused meanwhile
    assert Report.objects.get(id=report.id).publishing_status == "completed"


def test_only_an_accountant_and_up_deletes_a_report_in_xero(client, mapped, org):
    report, *_ = _published(mapped)
    manager = _member(mapped, "manager@test.com", EntityRole.SHOP_MANAGER)
    accountant = _member(mapped, "accountant@test.com", EntityRole.ACCOUNTANT)

    def may_delete(user):
        view = client.get(url(mapped.entity.id, f"/reports/{report.id}"), headers=headers(user.id)).json()
        row = client.get(url(mapped.entity.id, "/reports"), headers=headers(user.id)).json()["reports"][0]
        return view["permissions"]["delete"], row["permissions"]["delete"]

    assert may_delete(manager) == (False, False)
    assert may_delete(accountant) == (True, True)
    refused = _delete(client, mapped, report.id, user=manager)
    assert refused.status_code == 403
    assert refused.json() == {"error": "Only an accountant or admin can delete a report that's in Xero."}
    assert Report.objects.filter(id=report.id).exists()
    assert _delete(client, mapped, report.id, user=accountant).status_code == 200


def test_a_report_never_in_xero_is_deleted_by_a_shop_manager_as_before(client, mapped, org):
    report = _report(mapped)
    manager = _member(mapped, "manager@test.com", EntityRole.SHOP_MANAGER)

    assert _delete(client, mapped, report.id, user=manager).status_code == 200
    assert org.calls == []


@pytest.mark.parametrize("record", ["none", "another organisation"])
def test_with_no_usable_record_the_report_is_deleted_with_no_xero_call(client, mapped, org, record):
    """Loaded from the old system, or published to another organisation: nothing to find in Xero.
    The app has warned that its entries stay there (owner, 2026-11-17)."""
    report = _report(mapped, xero_integrated=True, publishing_status="completed", status="published")
    if record == "another organisation":
        publish_record.record_object(report.id, "old-org", "invoices", "inv-old", object_type="INVOICE")

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 200, resp.json()
    assert org.calls == []
    assert not Report.objects.filter(id=report.id).exists()


def test_xero_needing_a_reconnect_keeps_the_report(client, mapped, org, monkeypatch):
    report, *_ = _published(mapped)

    def gone(entity_id):
        raise XeroTokenUnavailable(reconnect=True)

    monkeypatch.setattr(pub, "xero_token", gone)
    org.calls.clear()

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 400
    assert resp.json() == {"error": pub.RECONNECT, "reconnect": True}
    assert org.calls == []
    assert Report.objects.get(id=report.id).publishing_status == "completed"


def test_the_token_service_unreachable_keeps_the_report(client, mapped, org, monkeypatch):
    report, *_ = _published(mapped)

    def down(entity_id):
        raise XeroTokenUnavailable(reconnect=False)

    monkeypatch.setattr(pub, "xero_token", down)

    resp = _delete(client, mapped, report.id)

    assert resp.status_code == 503
    assert Report.objects.filter(id=report.id).exists()


def test_the_publish_sweep_still_removes_what_no_longer_applies(mapped, org):
    """The removal helpers now say why they failed; the publish's sweep reads only yes or no."""
    report, first, second, objects = _published(mapped)
    taxi = objects["expenses"][str(second.id)]["id"]
    second.delete()
    Report.objects.filter(id=report.id).update(expense_total=10)
    org.calls.clear()

    _run(mapped, report)

    assert org.sent("POST", f"BankTransactions/{taxi}") == [
        {"bankTransactions": [{"bankTransactionID": taxi, "status": "DELETED"}]}]
    assert str(second.id) not in publish_record.load_record(report.id)["objects"]["expenses"]
