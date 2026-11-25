"""Publishing to Xero (``petty_cash/routes/report.py``, ``services/publish.py``,
``petty_cash/xero/steps.py``), done here since Phase 3 of the Flask retirement.

Restated from Flask's tests:
- ``tests/test_report_publish_state.py`` and the publishing section of
  ``test_petty_cash_api_report.py``: the start, its refusals and the lock;
- ``tests/test_xero_report_republish.py`` and ``test_xero_expense_receipts.py``: the steps, a
  republish updating in place, the sweep and the receipts.

Xero is faked at ``requests`` (every call recorded) and the token service at ``xero_token``. The
worker runs in the test's own thread (``run_publish``); the start's thread is only recorded.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
import requests
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    Attachment,
    CurrencyInfo,
    Entity,
    EntityPettycashSettings,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    UserEntity,
    XeroContactSync,
)

from core.xero_tokens import XeroToken, XeroTokenUnavailable
from petty_cash.services import publish as pub
from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3
from petty_cash.xero import publish_record, receipts, steps

pytestmark = pytest.mark.django_db
ORG = "org-1"
REASON = {"text": "No account code for Fees", "resolved": True}


class _Response:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeXero:
    """Xero's accounting and files APIs. ``answers[(method, path prefix)]`` overrides an answer
    (a ``_Response`` or an exception); every call lands in ``calls`` as ``(method, path, body)``."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.answers: dict[tuple[str, str], object] = {}
        self.attached: dict[str, list[dict]] = {}  # transaction id -> its files
        self.found: list[dict] = []  # Xero's answer to a contact name search
        self.n = 0

    def _path(self, url):
        for base in ("https://api.xero.com/api.xro/2.0/", "https://api.xero.com/files.xro/1.0/",
                     "https://api.xero.com/"):
            if url.startswith(base):
                return url[len(base):]
        raise AssertionError(f"unexpected URL {url}")

    def _override(self, method, path):
        for (m, prefix), answer in self.answers.items():
            if m == method and path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return None

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    def request(self, method, url, body):
        path = self._path(url)
        self.calls.append((method, path, body))
        answer = self._override(method, path)
        if answer is not None:
            return answer
        if method == "GET" and path == "connections":
            return _Response(200, [{"tenantId": ORG}])
        if method == "GET" and path == "Organisation":
            return _Response(200, {"Organisations": [{"OrganisationID": ORG, "Name": "Acme Ltd",
                                                      "BaseCurrency": "HKD"}]})
        if method == "GET" and path.startswith("Associations/"):
            files = self.attached.get(path.split("/", 1)[1])
            return _Response(200, files) if files else _Response(404, [])
        if method == "GET" and path == "Contacts":
            return _Response(200, {"Contacts": self.found})
        if method == "GET" and path.startswith("Contacts/"):
            return _Response(200, {"Contacts": [{"ContactID": path.split("/", 1)[1], "ContactStatus": "ACTIVE"}]})
        if path.startswith("BankTransactions"):
            return _Response(200, {"BankTransactions": [{"BankTransactionID": path.split("/", 1)[1] if "/" in path
                                                         else self._id("btx")}]})
        if path.startswith("BankTransfers/"):
            return _Response(200, {})
        if path == "BankTransfers":
            return _Response(200, {"BankTransfers": [{"BankTransferID": self._id("btr")}]})
        if path.startswith("Invoices"):
            return _Response(200, {"Invoices": [{"InvoiceID": path.split("/", 1)[1] if "/" in path
                                                 else self._id("inv")}]})
        if path == "Contacts":
            return _Response(200, {"Contacts": [{"ContactID": self._id("con"), "Name": body["Contacts"][0]["Name"]}]})
        if path == "Files":
            return _Response(201, {"FileId": self._id("file")})
        if path.endswith("/Associations"):
            return _Response(200, {})
        if method == "DELETE":
            return _Response(204)
        raise AssertionError(f"unhandled {method} {path}")

    def get(self, url, headers=None, params=None, timeout=None, **_):
        return self.request("GET", url, params)

    def post(self, url, data=None, json=None, files=None, headers=None, timeout=None, **_):
        body = json if json is not None else (files and list(files)) or (data and __import__("json").loads(data))
        return self.request("POST", url, body)

    def put(self, url, data=None, headers=None, timeout=None, **_):
        return self.request("PUT", url, __import__("json").loads(data))

    def delete(self, url, headers=None, timeout=None, **_):
        return self.request("DELETE", url, None)

    def sent(self, method, prefix):
        return [b for m, p, b in self.calls if m == method and p.startswith(prefix)]


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    for verb in ("get", "post", "put", "delete"):
        monkeypatch.setattr(requests, verb, getattr(fake, verb))
    monkeypatch.setattr(pub, "xero_token", lambda entity_id: XeroToken(access_token="tok", xero_org_id=ORG))
    monkeypatch.setattr(receipts.storage, "read_receipt", lambda key, limit: b"receipt-bytes")
    monkeypatch.setattr(steps, "BATCH_PAUSE", 0)
    return fake


@pytest.fixture
def threads(monkeypatch) -> list:
    started = []

    class Recorder:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr(pub.threading, "Thread", Recorder)
    return started


@pytest.fixture
def mapped(shop):
    """A company connected to Xero with all nine Petty Cash slots mapped."""
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    shop.entity.refresh_from_db()

    def account(code, name, kind="BANK"):
        return AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                          name=name, type=kind, status="ACTIVE")

    def contact(xid, name):
        return XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id=xid, name=name)

    EntityPettycashSettings.objects.create(
        entity_id=shop.entity.id,
        pettycash_account_id=account("091", "Petty Cash").id, bank_account_id=account("090", "Deposit Bank").id,
        cash_sale_account_id=account("200", "Sales", "REVENUE").id,
        director_account_id=account("880", "Director", "EQUITY").id,
        discrepancy_account_id=account("420", "Cash Over Short", "EXPENSE").id,
        discrepancy_bank_account_id=account("092", "Discrepancy Bank").id,
        cash_sale_contact_id=contact("con-cash", "Cash Sale").id,
        director_contact_id=contact("con-dir", "Olive Director").id,
        discrepancy_contact_id=contact("con-disc", "Discrepancy").id,
    )
    return shop


def _report(shop, **fields) -> Report:
    values = {"opening_balance": 100, "closing_balance": 0, "completed_sections": ["submitted"]}
    values.update(fields)
    return make_report(shop.entity, D3, created_by=shop.owner.id, **values)


def _line(shop, report, item, amount, *, code="429", contact="con-sup", receipt=None):
    account = (AccountInfo.objects.filter(entity_id=shop.entity.id, xero_code=code).first()
               or AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                             name=f"Account {code}", type="EXPENSE", status="ACTIVE"))
    supplier = (XeroContactSync.objects.filter(entity_id=shop.entity.id, xero_contact_id=contact).first()
                or XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id=contact, name="ABC Supplies"))
    line = ReportExpense.objects.create(report_id=report.id, item=item, amount=amount, account_id=account.id,
                                        contact_id=supplier.id, description=f"{item} receipt")
    if receipt:
        attachment = Attachment.objects.create(id=uuid.uuid4(), original_name="r.png", stored_name="r.png",
                                               file_path=receipt, file_extension="png")
        ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id, sort_order=0)
    return line


def _full_day(shop):
    """A day with every step: a company withdrawal, cash sales, two receipted expenses, a deposit
    and a short count."""
    report = _report(shop, cash_addition=50, cash_addition_type="company", cashsale_total=200, expense_total=30,
                     bank_deposit=150, discrepancy_amount=-5, discrepancy_type="short",
                     discrepancy_reason="Short by 5")
    first = _line(shop, report, "Coffee", 10, receipt="r/coffee.png")
    second = _line(shop, report, "Taxi", 20, receipt="r/taxi.png")
    return report, first, second


def _publish(client, shop, report_id, user=None):
    return client.post(url(shop.entity.id, f"/reports/{report_id}/publish"), headers=headers((user or shop.owner).id))


def _status(client, shop, report_id):
    return client.get(url(shop.entity.id, f"/reports/{report_id}/publishing-status"), headers=headers(shop.owner.id))


def _run(shop, report, prior=None):
    pub.run_publish(shop.entity.id, report.id, shop.owner.id, prior)
    report.refresh_from_db()
    return report


# ---- the start -------------------------------------------------------------------------------


def test_the_start_locks_the_report_and_starts_the_worker(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 202
    assert resp.json() == {"publishing_status": "publishing", "message": "Publishing to Xero. This can take a few minutes."}
    assert Report.objects.get(id=report.id).publishing_status == "publishing"
    assert [tuple(str(a) for a in args) for args in threads] == [
        (str(mapped.entity.id), str(report.id), str(mapped.owner.id), "unpublished")]
    assert ("GET", "Organisation", None) in xero.calls  # the currencies are compared first


def test_a_xero_organisation_in_another_currency_is_refused_before_anything_is_sent(client, mapped, xero, threads):
    """Minty's currency and the organisation's base currency differ (2026-11-24): nothing goes to
    Xero, and the sentence says what to change - Xero can't change its base currency."""
    xero.answers[("GET", "Organisation")] = _Response(200, {"Organisations": [
        {"OrganisationID": ORG, "Name": "Acme Ltd", "BaseCurrency": "USD"}]})
    report, *_ = _full_day(mapped)

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Xero uses USD for Acme Ltd, but this company uses HKD in Minty, so nothing was "
                                    "sent. Disconnect Xero in Settings, change the company's currency to USD, then "
                                    "connect again."}
    assert threads == [] and Report.objects.get(id=report.id).publishing_status != "publishing"
    assert [(m, p) for m, p, _ in xero.calls if m != "GET"] == []


@pytest.mark.parametrize("answer", [requests.ConnectionError("down"), _Response(500, {"Message": "busy"}),
                                    _Response(200, {"Organisations": [{"Name": "Acme Ltd"}]})])
def test_an_organisation_xero_cant_tell_the_currency_of_still_publishes(client, mapped, xero, threads, answer):
    """The read failing only logs: every entry carries the company's currency, so Xero itself refuses
    a currency its organisation doesn't use."""
    xero.answers[("GET", "Organisation")] = answer
    report, *_ = _full_day(mapped)
    assert _publish(client, mapped, report.id).status_code == 202


def test_a_failed_publish_starts_again_knowing_it_failed(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="failed")
    assert _publish(client, mapped, report.id).status_code == 202
    assert threads[0][3] == "failed"


def test_a_report_already_publishing_is_refused(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="publishing")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409 and resp.json() == {"error": pub.ALREADY}
    assert threads == []


@pytest.mark.parametrize("reconnect, status, body", [
    (True, 400, {"error": pub.RECONNECT, "reconnect": True}),
    (False, 503, {"error": pub.NO_TOKEN_SERVICE}),
])
def test_no_token_is_refused_before_anything(client, mapped, xero, threads, monkeypatch, reconnect, status, body):
    def refused(entity_id):
        raise XeroTokenUnavailable(reconnect=reconnect)

    monkeypatch.setattr(pub, "xero_token", refused)
    report, *_ = _full_day(mapped)

    resp = _publish(client, mapped, report.id)

    assert (resp.status_code, resp.json()) == (status, body)
    assert Report.objects.get(id=report.id).publishing_status != "publishing" and threads == []


def test_incomplete_xero_settings_are_refused(client, shop, xero, threads):
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    report = _report(shop)
    resp = _publish(client, shop, report.id)
    assert (resp.status_code, resp.json()) == (400, {"error": pub.INCOMPLETE})


def test_a_draft_is_refused(client, mapped, xero, threads):
    report = _report(mapped, status="draft")
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (409, {"error": pub.DRAFT})


def test_a_system_account_line_is_refused(client, mapped, xero, threads):
    report = _report(mapped, expense_total=10)
    _line(mapped, report, "Rounding", 10, code="497")
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (400, {"error": "System accounts are not allowed to be published to Xero."})


def test_a_company_not_marked_connected_is_re_synced_from_xero(client, mapped, xero, threads):
    Entity.objects.filter(id=mapped.entity.id).update(status="disconnected")
    report, *_ = _full_day(mapped)

    assert _publish(client, mapped, report.id).status_code == 202
    assert Entity.objects.get(id=mapped.entity.id).status == "connected"
    assert ("GET", "connections", None) in xero.calls


def test_an_unknown_or_another_companys_report_is_404(client, mapped, xero, threads):
    stranger = make_user("stranger@test.com")
    theirs = make_report(make_entity(stranger, name="Their Shop"), D3, created_by=stranger.id)
    for report_id in (uuid.uuid4(), theirs.id, "not-a-uuid"):
        assert _publish(client, mapped, report_id).status_code == 404
    assert xero.calls == [] and threads == []


def test_a_person_without_the_publish_right_is_403(client, mapped, xero, threads):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=mapped.entity.id, role=EntityRole.CASHIER, approved=True)
    report, *_ = _full_day(mapped)
    resp = _publish(client, mapped, report.id, user=cashier)
    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have permission to publish reports for this company."}
    assert xero.calls == [] and threads == []


# ---- the worker: a whole day -----------------------------------------------------------------


def test_a_whole_day_goes_into_xero_and_the_report_reads_published(mapped, xero):
    report, first, second = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="publishing")

    report = _run(mapped, report)

    assert (report.publishing_status, report.status, report.xero_integrated) == ("completed", "published", True)
    assert report.published_at is not None
    assert list(ReportHistory.objects.filter(report_id=report.id).values_list("action", "user_id")) == [
        ("published", mapped.owner.id)]

    withdrawal, deposit = xero.sent("POST", "BankTransfers")
    assert withdrawal == {"BankTransfers": [{
        "FromBankAccount": {"AccountID": "acc-090", "CurrencyCode": "HKD"},
        "ToBankAccount": {"AccountID": "acc-091", "CurrencyCode": "HKD"},
        "Date": D3.isoformat(), "Amount": 50.0, "Reference": f"MT{D3:%Y%m%d}Withdrawal"}]}
    assert deposit["BankTransfers"][0]["Reference"] == f"MT{D3:%Y%m%d}Deposit"
    assert (deposit["BankTransfers"][0]["FromBankAccount"]["AccountID"], deposit["BankTransfers"][0]["Amount"]) == (
        "acc-091", 150.0)

    invoice, = xero.sent("PUT", "Invoices")
    assert invoice["Invoices"][0]["Contact"] == {"ContactID": "con-cash"}
    assert invoice["Invoices"][0]["CurrencyCode"] == "HKD"
    assert invoice["Invoices"][0]["LineItems"] == [{"Description": f"MT{D3:%Y%m%d}Cash Sales", "Quantity": 1,
                                                    "UnitAmount": 200.0, "AccountCode": "200", "TaxType": "NONE",
                                                    "LineAmount": 200.0}]

    # Each expense line's description in Xero is what the person typed, not the account name.
    by_item = {b["bankTransactions"][0]["lineItems"][0]["description"]: b for b in xero.sent("POST", "BankTransactions")}
    coffee, taxi, short = by_item["Coffee receipt"], by_item["Taxi receipt"], by_item["Short by 5"]
    assert coffee["bankTransactions"][0] == {
        "type": "SPEND", "Date": D3.isoformat(), "bankAccount": {"accountID": "acc-091"},
        "Reference": f"MT{D3:%Y%m%d}Expense", "contact": {"contactID": "con-sup"}, "currencyCode": "HKD",
        "lineItems": [{"description": "Coffee receipt", "unitAmount": 10.0, "accountCode": "429", "taxType": "NONE"}]}
    assert taxi["bankTransactions"][0]["lineItems"][0]["unitAmount"] == 20.0
    assert short["bankTransactions"][0]["type"] == "SPEND"
    assert short["bankTransactions"][0]["currencyCode"] == "HKD"
    assert short["bankTransactions"][0]["Reference"] == f"MT{D3:%Y%m%d}Discrepancy"
    assert short["bankTransactions"][0]["lineItems"] == [{"description": "Short by 5", "unitAmount": 5.0,
                                                           "accountCode": "420", "taxType": "NONE"}]
    record = publish_record.load_record(report.id)
    record_ids = record["objects"]
    assert set(record["objects"]) == {"withdrawal_from", "invoices", "expenses", "deposit", "discrepancy"}
    assert set(record["objects"]["expenses"]) == {str(first.id), str(second.id)}
    assert record["org"] == ORG
    # Each line's receipt, named after its description (``safe_stem``, as Flask names it), on its own transaction.
    assert sorted(b[0] for m, p, b in xero.calls if (m, p) == ("POST", "Files")) == ["COFFEE_RECEIPT.png",
                                                                                    "TAXI_RECEIPT.png"]
    assert sorted(b["ObjectId"] for m, p, b in xero.calls if m == "POST" and p.endswith("/Associations")) == sorted(
        [record_ids["expenses"][str(first.id)]["id"], record_ids["expenses"][str(second.id)]["id"]])


@pytest.mark.parametrize("description", ["", None, "   ", "No description", "-"])
def test_a_line_with_no_description_goes_into_xero_under_its_account_name(mapped, xero, description):
    """Its account's name NOW (the user's rule, 2026-11-24): renamed since the line was saved, the new one."""
    report = _report(mapped, expense_total=10)
    line = _line(mapped, report, "Staff Meals", 10)
    AccountInfo.objects.filter(id=line.account_id).update(name="Staff Welfare")
    ReportExpense.objects.filter(id=line.id).update(description=description)
    Report.objects.filter(id=report.id).update(publishing_status="publishing")

    _run(mapped, report)

    sent, = xero.sent("POST", "BankTransactions")
    assert sent["bankTransactions"][0]["lineItems"][0]["description"] == "Staff Welfare"


def test_an_over_count_receives_and_a_personal_withdrawal_names_the_director(mapped, xero):
    report = _report(mapped, cash_addition=40, cash_addition_type="personal", discrepancy_amount=3,
                     discrepancy_type="over", discrepancy_reason="Over by 3")

    _run(mapped, report)

    personal, over = xero.sent("POST", "BankTransactions")
    assert personal["bankTransactions"][0]["type"] == "RECEIVE"
    assert personal["bankTransactions"][0]["contact"] == {"contactID": "con-dir"}
    assert personal["bankTransactions"][0]["lineItems"] == [{"description": "Amount due to Olive Director",
                                                              "quantity": 1, "unitAmount": 40.0,
                                                              "accountCode": "880", "taxType": "NONE"}]
    assert over["bankTransactions"][0]["type"] == "RECEIVE"


def test_the_transfers_are_in_the_companys_currency(mapped, xero):
    sgd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="SGD", currency_name="SGD", symbol="$",
                                      decimal_places=2)
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=sgd.id)
    report = _run(mapped, _report(mapped, bank_deposit=150))
    assert xero.sent("POST", "BankTransfers")[0]["BankTransfers"][0]["ToBankAccount"]["CurrencyCode"] == "SGD"
    assert report.publishing_status == "completed"


def test_every_entry_is_in_the_companys_currency(mapped, xero):
    """Expenses, the counted difference and cash sales name the currency too (2026-11-24): without
    it Xero books the amount in the bank account's or the organisation's own currency, silently."""
    sgd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="SGD", currency_name="SGD", symbol="$",
                                      decimal_places=2)
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=sgd.id)
    report, *_ = _full_day(mapped)
    _run(mapped, report)
    assert {b["bankTransactions"][0]["currencyCode"] for b in xero.sent("POST", "BankTransactions")} == {"SGD"}
    assert [b["Invoices"][0]["CurrencyCode"] for b in xero.sent("PUT", "Invoices")] == ["SGD"]


def test_no_currency_fails_the_transfers_with_a_reason_never_a_silent_hkd(mapped, xero):
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=None)
    report = _run(mapped, _report(mapped, bank_deposit=150))
    assert report.publishing_status == "failed" and xero.sent("POST", "BankTransfers") == []
    failure = ReportHistory.objects.get(report_id=report.id, action="publish_failed")
    assert [i["text"] for i in json.loads(failure.new_value)] == ["Deposit — the company's currency is not chosen"]


def test_no_currency_sends_no_expense_or_sales_either(mapped, xero):
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=None)
    report = _report(mapped, cashsale_total=200, expense_total=10)
    _line(mapped, report, "Coffee", 10)
    report = _run(mapped, report)
    assert report.publishing_status == "failed"
    assert xero.sent("POST", "BankTransactions") == [] and xero.sent("PUT", "Invoices") == []
    failure = ReportHistory.objects.get(report_id=report.id, action="publish_failed")
    texts = [i["text"] for i in json.loads(failure.new_value)]
    assert len(texts) == 2 and all(t.endswith("the company's currency is not chosen") for t in texts)


# ---- failures --------------------------------------------------------------------------------


def test_a_refused_line_fails_the_publish_with_its_reason_and_the_rest_is_recorded(mapped, xero):
    report, first, second = _full_day(mapped)
    calls = {"n": 0}

    def second_refused(method, url, body, real=xero.request):
        path = xero._path(url)
        if path == "BankTransactions" and body["bankTransactions"][0]["lineItems"][0]["description"] == "Taxi receipt":
            xero.calls.append((method, path, body))
            calls["n"] += 1
            return _Response(400, {"Elements": [{"ValidationErrors": [{"Message": "The contact is archived"}]}]})
        return real(method, url, body)

    xero.request = second_refused
    report = _run(mapped, report)

    assert (report.publishing_status, report.xero_integrated) == ("failed", False)
    items = json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value)
    # the line's account as the chart names it now (the user's rule, 2026-11-24), not its saved item
    assert items == [{"text": "Expense 'Account 429' — contact is archived in Xero", "scope": "expense",
                      "expense_id": str(second.id), "deps": [], "failed_contact_id": "con-sup",
                      "module": "expenses"}]
    assert set(publish_record.load_record(report.id)["objects"]["expenses"]) == {str(first.id)}


def test_a_selective_re_publish_retries_only_what_failed(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)  # everything in Xero
    ReportHistory.objects.create(report_id=report.id, action="publish_failed", new_value=json.dumps(
        [{"text": "Expense 'Taxi' — x", "scope": "expense", "expense_id": str(second.id), "module": "expenses"}]))
    xero.calls.clear()

    _run(mapped, report, prior="failed")

    assert [b["bankTransactions"][0]["lineItems"][0]["description"] for b in xero.sent("POST", "BankTransactions")] == [
        "Taxi receipt"]
    assert xero.sent("PUT", "Invoices") == [] and xero.sent("POST", "BankTransfers") == []


def test_a_rejected_token_stops_the_run_with_one_reason(mapped, xero):
    report, *_ = _full_day(mapped)
    xero.answers[("POST", "BankTransfers")] = _Response(401, {"Title": "Unauthorized"})

    report = _run(mapped, report)

    assert report.publishing_status == "failed"
    items = json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value)
    assert [i["text"] for i in items] == ["Xero connection has expired"]
    assert xero.sent("PUT", "Invoices") == []  # stopped after the withdrawal


@pytest.mark.parametrize("reconnect, reason", [(True, "Xero connection has expired"),
                                               (False, "could not reach Xero, please try again")])
def test_a_worker_with_no_token_fails_with_a_reason(mapped, xero, monkeypatch, reconnect, reason):
    def refused(entity_id):
        raise XeroTokenUnavailable(reconnect=reconnect)

    monkeypatch.setattr(pub, "xero_token", refused)
    report = _run(mapped, _report(mapped, bank_deposit=10))
    assert report.publishing_status == "failed"
    assert json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value) == [
        {"text": reason}]


def test_a_crash_never_leaves_the_report_publishing(mapped, xero, monkeypatch):
    report = _report(mapped, bank_deposit=10, publishing_status="publishing")
    monkeypatch.setattr(pub, "publish_report", lambda *a, **k: 1 / 0)
    assert _run(mapped, report).publishing_status == "failed"


# ---- republishing ----------------------------------------------------------------------------


def test_a_republish_updates_what_this_report_made_instead_of_duplicating(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)
    before = publish_record.load_record(report.id)["objects"]
    xero.calls.clear()

    _run(mapped, report)

    paths = [p for m, p, _ in xero.calls if m in ("POST", "PUT") and not p.startswith(("Files", "Contacts"))]
    assert f"BankTransactions/{before['expenses'][str(first.id)]['id']}" in paths
    assert f"Invoices/{before['invoices']['id']}" in paths and ("PUT", "Invoices") not in [(m, p) for m, p, _ in xero.calls]
    # A transfer can't be edited: the recorded one is deleted, then made again.
    assert f"BankTransfers/{before['deposit']['id']}" in paths
    assert xero.sent("POST", f"BankTransfers/{before['deposit']['id']}") == [{"Status": "DELETED"}]


def test_a_record_from_another_xero_organisation_is_ignored(mapped, xero):
    report = _report(mapped, cashsale_total=200)
    publish_record.record_object(report.id, "old-org", "invoices", "inv-old", object_type="INVOICE")
    _run(mapped, report)
    assert len(xero.sent("PUT", "Invoices")) == 1 and xero.sent("POST", "Invoices/inv-old") == []


def test_what_no_longer_applies_comes_out_of_xero(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)
    before = publish_record.load_record(report.id)["objects"]
    taxi = before["expenses"][str(second.id)]["id"]
    second.delete()
    Report.objects.filter(id=report.id).update(bank_deposit=0, expense_total=10)
    xero.calls.clear()

    _run(mapped, report)

    assert xero.sent("POST", f"BankTransactions/{taxi}") == [
        {"bankTransactions": [{"bankTransactionID": taxi, "status": "DELETED"}]}]
    assert xero.sent("POST", f"BankTransfers/{before['deposit']['id']}") == [{"Status": "DELETED"}]
    after = publish_record.load_record(report.id)["objects"]
    assert "deposit" not in after and set(after["expenses"]) == {str(first.id)}


# ---- receipts and contacts -------------------------------------------------------------------


def test_an_unchanged_receipt_is_left_alone_and_a_changed_one_replaced_after(mapped, xero):
    report = _report(mapped, expense_total=10)
    _line(mapped, report, "Coffee", 10, receipt="r/coffee.png")
    _run(mapped, report)
    transaction_id = next(iter(publish_record.load_record(report.id)["objects"]["expenses"].values()))["id"]
    xero.attached[transaction_id] = [{"FileId": "file-old", "Name": "COFFEE_RECEIPT.png", "Size": len(b"receipt-bytes")}]
    xero.calls.clear()

    _run(mapped, report)
    assert [p for m, p, _ in xero.calls if m == "POST" and p.startswith("Files")] == []  # unchanged: nothing sent

    xero.attached[transaction_id] = [{"FileId": "file-old", "Name": "COFFEE_RECEIPT.png", "Size": 1}]
    xero.calls.clear()
    _run(mapped, report)
    order = [(m, p) for m, p, _ in xero.calls if p.startswith("Files")]
    assert order[0] == ("POST", "Files") and order[-1] == ("DELETE", "Files/file-old")


def test_a_supplier_xero_lost_is_made_again_on_the_same_row(mapped, xero):
    """Xero no longer has the contact (404): it is looked for by name, else made again as a
    supplier, and the new id goes on the same Minty contact - no second row."""
    report = _report(mapped, expense_total=10)
    line = _line(mapped, report, "Coffee", 10, contact="con-gone")
    rows = XeroContactSync.objects.filter(entity_id=mapped.entity.id).count()
    xero.answers[("GET", "Contacts/con-gone")] = _Response(404, {})

    _run(mapped, report)

    made, = xero.sent("POST", "Contacts")
    assert made == {"Contacts": [{"Name": "ABC Supplies", "IsCustomer": False, "IsSupplier": True}]}
    line.refresh_from_db()
    new_id = XeroContactSync.objects.get(id=line.contact_id).xero_contact_id
    assert new_id != "con-gone"
    assert XeroContactSync.objects.filter(entity_id=mapped.entity.id).count() == rows
    assert xero.sent("POST", "BankTransactions")[0]["bankTransactions"][0]["contact"] == {"contactID": new_id}


# ---- contacts at publish (owner's decisions doc C1, 2026-11-19) --------------------------------


def _own_line(shop, report, item, amount, name):
    """A line whose supplier was made in Minty and isn't in Xero yet."""
    line = _line(shop, report, item, amount)
    own = XeroContactSync.objects.create(entity_id=shop.entity.id, name=name, source="user")
    ReportExpense.objects.filter(id=line.id).update(contact_id=own.id)
    return line, own


def test_a_contact_xero_has_by_name_is_linked_at_publish_never_made(mapped, xero):
    report = _report(mapped, expense_total=10)
    _own_line(mapped, report, "Coffee", 10, "Kee Wah")
    xero.found = [{"ContactID": "con-kee", "Name": "KEE  WAH", "ContactStatus": "ACTIVE"}]

    report = _run(mapped, report)

    assert report.publishing_status == "completed"
    assert xero.sent("POST", "Contacts") == []
    own = XeroContactSync.objects.get(entity_id=mapped.entity.id, name="Kee Wah")
    assert (own.xero_contact_id, own.xero_org_id) == ("con-kee", ORG)
    assert xero.sent("POST", "BankTransactions")[0]["bankTransactions"][0]["contact"] == {"contactID": "con-kee"}


def test_a_contact_xero_hasnt_is_made_there_as_a_supplier_and_kept_on_the_same_row(mapped, xero):
    report = _report(mapped, expense_total=10)
    _own_line(mapped, report, "Coffee", 10, "Kee Wah")
    rows = XeroContactSync.objects.filter(entity_id=mapped.entity.id).count()

    _run(mapped, report)

    assert xero.sent("POST", "Contacts") == [{"Contacts": [{"Name": "Kee Wah", "IsCustomer": False,
                                                            "IsSupplier": True}]}]
    own = XeroContactSync.objects.get(entity_id=mapped.entity.id, name="Kee Wah")
    assert own.xero_contact_id and XeroContactSync.objects.filter(entity_id=mapped.entity.id).count() == rows
    sent = xero.sent("POST", "BankTransactions")[0]["bankTransactions"][0]
    assert sent["contact"] == {"contactID": own.xero_contact_id}


def test_a_daily_closing_contact_not_in_xero_is_linked_too(mapped, xero):
    """The cash sale contact made in Minty: its invoice names the Xero contact found by name, never
    an empty ContactID."""
    XeroContactSync.objects.filter(entity_id=mapped.entity.id, xero_contact_id="con-cash").update(
        xero_contact_id=None, source="user")
    xero.found = [{"ContactID": "con-walkin", "Name": "Cash Sale", "ContactStatus": "ACTIVE"}]
    report = _report(mapped, cashsale_total=200)

    _run(mapped, report)

    invoice = xero.sent("PUT", "Invoices")[0]["Invoices"][0]
    assert invoice["Contact"] == {"ContactID": "con-walkin"}


def test_xero_not_answering_for_a_contact_fails_that_entry_never_posts_it_without_one(mapped, xero):
    report = _report(mapped, expense_total=10)
    _own_line(mapped, report, "Coffee", 10, "Kee Wah")
    xero.answers[("GET", "Contacts")] = requests.ConnectionError("down")

    report = _run(mapped, report)

    assert report.publishing_status == "failed"
    assert xero.sent("POST", "BankTransactions") == [] and xero.sent("POST", "Contacts") == []


# ---- GET /reports/{id}/publishing-status -----------------------------------------------------


def test_a_publishing_report_says_so(client, mapped, xero):
    report = _report(mapped, publishing_status="publishing")
    body = _status(client, mapped, report.id).json()
    assert (body["publishing_status"], body["status_label"], body["reasons"]) == ("publishing", "Publishing to Xero", [])
    assert xero.calls == []


def test_a_failed_publish_brings_its_reasons(client, mapped, xero):
    report = _report(mapped, publishing_status="failed")
    # The publish's stored reason: a line that has since been removed, so it reads resolved.
    ReportHistory.objects.create(report_id=report.id, action="publish_failed",
                                 new_value=json.dumps([{"text": REASON["text"], "scope": "expense",
                                                        "expense_id": str(uuid.uuid4())}]))
    body = _status(client, mapped, report.id).json()
    assert (body["publishing_status"], body["status_label"], body["status_tone"]) == ("failed", "Publish failed", "danger")
    assert body["reasons"] == [REASON]


def test_a_report_in_xero_reads_published(client, mapped, xero):
    report = _report(mapped, status="published", publishing_status="completed", xero_integrated=True,
                     published_at=datetime(2026, 10, 14, 2, 30, tzinfo=UTC))
    body = _status(client, mapped, report.id).json()
    assert (body["status_label"], body["xero_integrated"], body["published_at"]) == (
        "Published to Xero", True, "2026-10-14T10:30:00+08:00")


def test_another_companys_report_status_is_404(client, mapped, xero):
    stranger = make_user("stranger@test.com")
    theirs = make_report(make_entity(stranger, name="Their Shop"), D3, created_by=stranger.id)
    assert _status(client, mapped, theirs.id).status_code == 404


# ---- Phase 3 of the Xero matching (owner, 2026-11-12): a stale match is refused, named ----------------


def _copies(shop, *guids):
    """The organisation's accounts as the sync's copies hold them."""
    from django.utils import timezone
    from minty_db.models import XeroAccount

    for guid in guids:
        XeroAccount.objects.create(entity_id=shop.entity.id, xero_org_id=ORG, xero_guid=guid, name=guid,
                                   xero_type="EXPENSE", status="ACTIVE", synced_at=timezone.now())


def test_an_account_not_matched_to_this_organisation_is_refused_by_name(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    held = AccountInfo.objects.filter(entity_id=mapped.entity.id).exclude(xero_code="429")
    _copies(mapped, *held.values_list("xero_account_id", flat=True))
    AccountInfo.objects.filter(entity_id=mapped.entity.id, xero_code="429").update(xero_account_id="old-429")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Account 429 (429) isn't matched to your Xero organisation. Match it in "
                                    "Settings, under Integration, then publish again."}
    assert threads == [] and Report.objects.get(id=report.id).publishing_status != "publishing"


def test_an_account_archived_in_xero_is_refused_by_name(client, mapped, xero, threads):
    from minty_db.models import XeroAccount

    report, *_ = _full_day(mapped)
    _copies(mapped, *AccountInfo.objects.filter(entity_id=mapped.entity.id).values_list("xero_account_id", flat=True))
    XeroAccount.objects.filter(entity_id=mapped.entity.id, xero_guid="acc-429").update(status="ARCHIVED")
    xero.answers[("GET", "Accounts/acc-429")] = _Response(200, {"Accounts": [{"AccountID": "acc-429",
                                                                              "Status": "ARCHIVED"}]})

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Account 429 (429) is archived in Xero. Please check it in Xero, then map this "
                                    "account to the one you want to use (Settings, under General), and publish again."}
    assert ("GET", "Accounts/acc-429", None) in xero.calls


@pytest.mark.parametrize("stored", ["ARCHIVED", "DELETED"])
def test_an_account_restored_in_xero_since_the_last_sync_publishes(client, mapped, xero, threads, stored):
    """2026-11-18, Dine at Venus 2 - Demo: Office Supply was restored in Xero after the last sync, and
    every publish still said it was archived. The publish asks Xero about a stored archived account
    (the sync copies only ACTIVE ones, so an archived one reads DELETED) and takes its answer."""
    from minty_db.models import XeroAccount

    report, *_ = _full_day(mapped)
    _copies(mapped, *AccountInfo.objects.filter(entity_id=mapped.entity.id).values_list("xero_account_id", flat=True))
    XeroAccount.objects.filter(entity_id=mapped.entity.id, xero_guid="acc-429").update(status=stored)
    AccountInfo.objects.filter(entity_id=mapped.entity.id, xero_code="429").update(status="ARCHIVED")
    xero.answers[("GET", "Accounts/acc-429")] = _Response(200, {"Accounts": [{"AccountID": "acc-429",
                                                                              "Status": "ACTIVE"}]})

    assert _publish(client, mapped, report.id).status_code == 202
    assert XeroAccount.objects.get(entity_id=mapped.entity.id, xero_guid="acc-429").status == "ACTIVE"
    assert AccountInfo.objects.get(entity_id=mapped.entity.id, xero_code="429").status == "ACTIVE"
    assert len(threads) == 1


def test_an_archived_account_xero_cannot_be_asked_about_keeps_the_stored_answer(client, mapped, xero, threads):
    from minty_db.models import XeroAccount

    report, *_ = _full_day(mapped)
    _copies(mapped, *AccountInfo.objects.filter(entity_id=mapped.entity.id).values_list("xero_account_id", flat=True))
    XeroAccount.objects.filter(entity_id=mapped.entity.id, xero_guid="acc-429").update(status="ARCHIVED")
    xero.answers[("GET", "Accounts/acc-429")] = requests.ConnectionError("down")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert "Account 429 (429) is archived in Xero." in resp.json()["error"]
    assert threads == []


def test_a_contact_the_day_needs_archived_in_xero_is_refused_by_name(client, mapped, xero, threads):
    """2026-11-17: Company of Friends' cash sale contact was archived in Xero; the publish started,
    and the cash sales failed as "account is archived". Now nothing is sent and the contact is named."""
    report, *_ = _full_day(mapped)  # cash sales, a company withdrawal and a short count
    XeroContactSync.objects.filter(entity_id=mapped.entity.id, xero_contact_id="con-cash").update(
        record_status="archived")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Cash Sale, your Cash Sales contact, is archived in Xero. Restore it in Xero "
                                    "or choose another in Settings, under Petty Cash Settings, then publish again."}
    assert threads == [] and Report.objects.get(id=report.id).publishing_status != "publishing"


def test_an_archived_contact_the_day_does_not_need_holds_nothing(client, mapped, xero, threads):
    """The director contact is used only for a personal withdrawal; this day's is from the company."""
    report, *_ = _full_day(mapped)
    XeroContactSync.objects.filter(entity_id=mapped.entity.id, xero_contact_id="con-dir").update(
        record_status="archived")
    assert _publish(client, mapped, report.id).status_code == 202


def test_xero_refusing_an_archived_contact_says_contact_not_account():
    """The cash sales step reads Xero's refusal with "account" as its subject; Xero's own sentence
    names the contact, and that wins (2026-11-17: it said "account is archived" for a contact)."""
    from petty_cash.xero.errors import translate_xero_error

    body = json.dumps({"Elements": [{"ValidationErrors": [
        {"Message": "The contact '7-Eleven' is archived. Restore the contact or choose another one."}]}]})
    assert translate_xero_error(400, body, subject="account") == "contact is archived in Xero"
    account = json.dumps({"Elements": [{"ValidationErrors": [{"Message": "Account code '260' has been archived"}]}]})
    assert translate_xero_error(400, account, subject="contact") == "account is archived in Xero"


@pytest.mark.parametrize("message", ["Organisation is not subscribed to currency SGD",
                                     "The currency code SGD is not valid for bank account 090"])
def test_xero_refusing_the_currency_says_what_to_change(message):
    """A currency refusal names the account too; it must not read as "account is no longer active"."""
    from petty_cash.xero.errors import XERO_CURRENCY, translate_xero_error

    body = json.dumps({"Elements": [{"ValidationErrors": [{"Message": message}]}]})
    assert translate_xero_error(400, body, subject="account") == XERO_CURRENCY


def test_a_supplier_of_an_older_organisation_is_not_refused(client, mapped, xero, threads):
    """The census (production's data, 2026-11-12): the publish re-creates a supplier Xero doesn't
    know, as it always has, so an older organisation's supplier isn't a reason to refuse."""
    report, *_ = _full_day(mapped)
    XeroContactSync.objects.filter(entity_id=mapped.entity.id, xero_contact_id="con-sup").update(
        xero_org_id="org-old")
    assert _publish(client, mapped, report.id).status_code == 202


def test_several_stale_matches_are_listed_one_a_line(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    _copies(mapped, "acc-091")
    resp = _publish(client, mapped, report.id)
    assert resp.status_code == 409
    lines = resp.json()["error"].split("\n")
    assert len(lines) > 1 and all("isn't matched to your Xero organisation" in line for line in lines)


@pytest.mark.parametrize("reason", ["first", "new_accounts", "new_org"])
def test_an_open_matching_holds_every_publish(client, mapped, xero, threads, reason):
    Entity.objects.filter(id=mapped.entity.id).update(xero_matching=reason)
    report, *_ = _full_day(mapped)
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (409, {"error": pub.STILL_MATCHING})


def test_a_change_of_matches_in_progress_does_not_hold_a_publish(client, mapped, xero, threads):
    Entity.objects.filter(id=mapped.entity.id).update(xero_matching="change")
    report, *_ = _full_day(mapped)
    assert _publish(client, mapped, report.id).status_code == 202



# ---- every sales account on the day's sales invoice (owner, 2026-11-17: "each sales should be tied") ----


def _sale(shop, report, name, kind, amount):
    from minty_db.models import ReportSale, SaleInfo

    row = SaleInfo.objects.create(sale_name=name, value_name=f"{name.lower()}_sales", type=kind, display_order=1)
    ReportSale.objects.create(report_id=report.id, sale_id=row.id, amount=amount)


def _sale_codes(shop):
    def account(code, name):
        return AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                          name=name, type="REVENUE", status="ACTIVE").id

    EntityPettycashSettings.objects.filter(entity_id=shop.entity.id).update(
        electronic_sale_account_id=account("201", "Credit Card Sales"),
        delivery_sale_account_id=account("202", "Online Sales"))


def test_electronic_and_delivery_sales_are_lines_on_the_days_sales_invoice(mapped, xero):
    _sale_codes(mapped)
    report = _report(mapped, cashsale_total=200, nocashsale_total=80)
    _sale(mapped, report, "Visa", "electronic", 50)
    _sale(mapped, report, "Keeta", "delivery", 30)
    _run(mapped, report)
    invoice, = xero.sent("PUT", "Invoices")
    lines = [(li["Description"], li["AccountCode"], li["LineAmount"]) for li in invoice["Invoices"][0]["LineItems"]]
    assert lines == [(f"MT{D3:%Y%m%d}Cash Sales", "200", 200.0),
                     (f"MT{D3:%Y%m%d}Electronic Payments", "201", 50.0),
                     (f"MT{D3:%Y%m%d}Delivery Sales", "202", 30.0)]
    assert invoice["Invoices"][0]["Contact"] == {"ContactID": "con-cash"}


def test_a_sales_account_with_no_code_yet_is_left_out_as_before(mapped, xero):
    report = _report(mapped, cashsale_total=200, nocashsale_total=50)
    _sale(mapped, report, "Visa", "electronic", 50)
    _run(mapped, report)
    invoice, = xero.sent("PUT", "Invoices")
    assert [li["AccountCode"] for li in invoice["Invoices"][0]["LineItems"]] == ["200"]


def test_a_day_with_card_sales_and_no_cash_still_sends_its_line(mapped, xero):
    _sale_codes(mapped)
    report = _report(mapped, cashsale_total=0, nocashsale_total=50)
    _sale(mapped, report, "Visa", "electronic", 50)
    report = _run(mapped, report)
    invoice, = xero.sent("PUT", "Invoices")
    assert [(li["AccountCode"], li["LineAmount"]) for li in invoice["Invoices"][0]["LineItems"]] == [("201", 50.0)]
    assert report.publishing_status == "completed"


# ---- a contact archived in Xero (owner's decisions doc C4, 2026-11-19) -------------------------------


def test_a_contact_xero_merged_follows_it_with_no_prompt(client, mapped, xero, threads, monkeypatch):
    from petty_cash.xero import contacts

    report = _report(mapped, expense_total=10)
    line = _line(mapped, report, "Coffee", 10, contact="con-old")
    xero.answers[("GET", "Contacts/con-old")] = _Response(200, {"Contacts": [
        {"ContactID": "con-old", "ContactStatus": "ARCHIVED", "MergedToContactID": "con-kept"}]})
    relinked = []
    monkeypatch.setattr(contacts, "relink", lambda *args, **kw: relinked.append(args) or {"merged": False})

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 202, resp.json()
    assert relinked == [(mapped.entity.id, line.contact_id, "con-kept", "merged_in_xero")]


def test_a_contact_archived_with_no_merge_asks_and_sends_nothing(client, mapped, xero, threads):
    report = _report(mapped, expense_total=10)
    line = _line(mapped, report, "Coffee", 10, contact="con-gone")
    xero.answers[("GET", "Contacts/con-gone")] = _Response(200, {"Contacts": [
        {"ContactID": "con-gone", "ContactStatus": "ARCHIVED"}]})

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "ABC Supplies is archived in Xero. Restore it in Xero, or map it to another contact now.",
        "archived_contacts": [{"id": str(line.contact_id), "name": "ABC Supplies"}]}
    assert threads == [] and xero.sent("POST", "Contacts") == []  # Minty never restores in Xero


def test_xero_not_answering_the_contact_check_stops_the_start(client, mapped, xero, threads):
    report = _report(mapped, expense_total=10)
    _line(mapped, report, "Coffee", 10, contact="con-x")
    xero.answers[("GET", "Contacts/con-x")] = requests.ConnectionError("down")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 503 and threads == []


def test_the_xero_contacts_a_publisher_can_map_to_are_the_active_copies(client, mapped):
    from minty_db.models import XeroContact

    XeroContact.objects.create(entity_id=mapped.entity.id, xero_org_id=ORG, xero_guid="x-b", name="Beta",
                               status="ACTIVE")
    XeroContact.objects.create(entity_id=mapped.entity.id, xero_org_id=ORG, xero_guid="x-a", name="Alpha",
                               status="ACTIVE")
    XeroContact.objects.create(entity_id=mapped.entity.id, xero_org_id=ORG, xero_guid="x-z", name="Gone",
                               status="ARCHIVED")
    resp = client.get(url(mapped.entity.id, "/xero/contacts"), headers=headers(mapped.owner.id))
    assert resp.json() == {"contacts": [{"xero_contact_id": "x-a", "name": "Alpha"},
                                        {"xero_contact_id": "x-b", "name": "Beta"}]}


def test_mapping_a_contact_now_relinks_it_through_accounts_api(client, mapped, monkeypatch):
    from minty_db.models import XeroContact

    from petty_cash.xero import contacts

    XeroContact.objects.create(entity_id=mapped.entity.id, xero_org_id=ORG, xero_guid="x-a", name="Alpha Ltd",
                               status="ACTIVE")
    kee = XeroContactSync.objects.create(entity_id=mapped.entity.id, name="Kee Wah", xero_contact_id="x-old")
    asked = []
    monkeypatch.setattr(contacts, "relink", lambda *args: asked.append(args) or {"merged": False,
                                                                               "contact_id": str(kee.id)})

    def link(body):
        return client.post(url(mapped.entity.id, f"/contacts/{kee.id}/xero-link"), body,
                           content_type="application/json", headers=headers(mapped.owner.id))

    assert link({"xero_contact_id": "x-unknown"}).status_code == 422, "only one of the company's Xero contacts"
    resp = link({"xero_contact_id": "x-a"})
    assert (resp.status_code, resp.json()["message"]) == (200, "Kee Wah now uses Alpha Ltd in Xero.")
    assert asked == [(mapped.entity.id, kee.id, "x-a", "user", mapped.owner.id)]


def test_a_line_whose_account_has_no_xero_code_is_refused_never_sent_as_200(mapped, xero):
    """Fail loudly (the user, 2026-11-24): an account with no Xero code (none yet, or a name in Xero's
    code field) used to go to Xero as account 200 without a word. The line is refused with its reason;
    the others go."""
    report = _report(mapped, expense_total=30)
    good = _line(mapped, report, "Coffee", 10)
    bad = _line(mapped, report, "Taxi", 20, code="471")
    AccountInfo.objects.filter(id=bad.account_id).update(xero_code=None, code="6100", name="Taxi Fares")
    report = _run(mapped, report)

    assert report.publishing_status == "failed"
    sent = [b["bankTransactions"][0]["lineItems"][0] for b in xero.sent("POST", "BankTransactions")]
    assert [li["accountCode"] for li in sent] == ["429"], "never a silent 200"
    items = json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value)
    assert [(i["text"], i["expense_id"]) for i in items] == [
        ("Expense 'Taxi Fares' — " + steps.NO_XERO_CODE.format(name="Taxi Fares"), str(bad.id))]
    assert set(publish_record.load_record(report.id)["objects"]["expenses"]) == {str(good.id)}


def test_mapping_now_to_a_xero_contact_another_contact_has_says_merge_first(client, mapped, monkeypatch):
    """Matching never merges (the user, 2026-11-25): accounts-api refuses, and its sentence is shown."""
    from minty_db.models import XeroContact

    from petty_cash.xero import contacts

    XeroContact.objects.create(entity_id=mapped.entity.id, xero_org_id=ORG, xero_guid="x-a", name="Alpha Ltd",
                               status="ACTIVE")
    kee = XeroContactSync.objects.create(entity_id=mapped.entity.id, name="Kee Wah", xero_contact_id="x-old")

    def refused(*args):
        raise contacts.RelinkRefused("Kee Wah Bakery already has that Xero contact. Merge the two in Settings first, or choose another contact.", 409)

    monkeypatch.setattr(contacts, "relink", refused)
    resp = client.post(url(mapped.entity.id, f"/contacts/{kee.id}/xero-link"), {"xero_contact_id": "x-a"},
                       content_type="application/json", headers=headers(mapped.owner.id))
    assert (resp.status_code, resp.json()["error"]) == (409, "Kee Wah Bakery already has that Xero contact. Merge the two in Settings first, or choose another contact.")
