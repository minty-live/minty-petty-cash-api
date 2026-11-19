"""A report's publish History (``GET /reports/{id}/history``, owner's H1, 2026-11-19): every publish
attempt on the report, newest first - when, who, published or failed, and a failure's issues.

Only the newest failed attempt is re-checked (``services/publish_reasons.py``, which asks Xero), and
only while the report is still failed; older failures show their stored issues with
``resolved: None``. Xero is stubbed as in ``test_publish_reasons.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import requests
from minty_db.enums import EntityRole
from minty_db.models import ReportExpense, ReportHistory, UserEntity, XeroContactSync

from core.xero_tokens import XeroToken
from petty_cash.services import publish_reasons as pr
from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3
from petty_cash.tests.test_publish_reasons import FakeXero

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 19, 2, 30, tzinfo=UTC)  # 10:30 in Hong Kong


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(pr, "xero_token", lambda entity_id: XeroToken(access_token="tok", xero_org_id="org-1"))
    return fake


def _row(report, action, items=None, *, user=None, at):
    row = ReportHistory.objects.create(
        id=uuid.uuid4(), report_id=report.id, user_id=user.id if user else None, action=action,
        new_value=items if isinstance(items, str) or items is None else json.dumps(items))
    ReportHistory.objects.filter(id=row.id).update(created_at=at)
    return row


def _get(client, shop, report_id, user=None):
    return client.get(url(shop.entity.id, f"/reports/{report_id}/history"), headers=headers((user or shop.owner).id))


def _report(shop, **fields):
    return make_report(shop.entity, D3, created_by=shop.owner.id, **fields)


def test_every_publish_attempt_newest_first_with_who_and_when(client, shop, xero):
    report = _report(shop, publishing_status="failed")
    sam = make_user("sam@test.com", first_name="Sam", last_name="Lee")
    _row(report, "published", user=shop.owner, at=NOW - timedelta(days=2))
    _row(report, "updated", "x", user=sam, at=NOW - timedelta(days=1, hours=1))  # an edit: not an attempt
    _row(report, "publish_failed", [{"text": "Xero connection has expired"}], user=sam, at=NOW - timedelta(days=1))
    _row(report, "publish_failed", [{"text": "No account code for Cleaning"}], user=shop.owner, at=NOW)

    resp = _get(client, shop, report.id)

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"history": [
        {"at": "2026-11-19T10:30:00+08:00", "user_name": "Olive Owner", "action": "publish_failed",
         "details": [{"text": "No account code for Cleaning", "resolved": False}]},
        {"at": "2026-11-18T10:30:00+08:00", "user_name": "Sam Lee", "action": "publish_failed",
         "details": [{"text": "Xero connection has expired", "resolved": None}]},
        {"at": "2026-11-17T10:30:00+08:00", "user_name": "Olive Owner", "action": "published", "details": []},
    ]}


def test_old_rows_are_read_as_attempts(client, shop, xero):
    """Plain-string lists, bad JSON and the reason in the action itself (``publish_failed: ...``);
    a row whose person is gone has no name."""
    report = _report(shop, publishing_status="completed", xero_integrated=True)
    _row(report, "publish_failed: Deposit - bank account missing", at=NOW - timedelta(hours=3))
    _row(report, "publish_failed", ["Expense 'Coffee' - contact is archived in Xero"], at=NOW - timedelta(hours=2))
    _row(report, "publish_failed", "Plain words, not JSON", at=NOW - timedelta(hours=1))
    _row(report, "published", at=NOW)

    history = _get(client, shop, report.id).json()["history"]

    assert [(h["action"], h["user_name"], h["details"]) for h in history] == [
        ("published", None, []),
        ("publish_failed", None, [{"text": "Plain words, not JSON", "resolved": None}]),
        ("publish_failed", None, [{"text": "Expense 'Coffee' - contact is archived in Xero", "resolved": None}]),
        ("publish_failed", None, [{"text": "Deposit - bank account missing", "resolved": None}]),
    ]
    assert xero.asked == []  # published since: nothing is re-checked


def test_only_the_newest_failed_attempt_is_re_checked(client, shop, xero):
    report = _report(shop, publishing_status="failed")
    contact = XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-1", name="ABC")
    old_contact = XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-old", name="Old")
    xero.statuses.update({"con-1": "ACTIVE", "con-old": "ACTIVE"})
    line = ReportExpense.objects.create(report_id=report.id, item="Coffee", amount=5, contact_id=contact.id)
    old = ReportExpense.objects.create(report_id=report.id, item="Tea", amount=5, contact_id=old_contact.id)
    _row(report, "publish_failed", [{"text": "Expense 'Tea' - x", "expense_id": str(old.id)}],
         at=NOW - timedelta(hours=1))
    _row(report, "publish_failed", [{"text": "Expense 'Coffee' - x", "expense_id": str(line.id)}], at=NOW)

    history = _get(client, shop, report.id).json()["history"]

    assert [h["details"] for h in history] == [
        [{"text": "Expense 'Coffee' - x", "resolved": True}],
        [{"text": "Expense 'Tea' - x", "resolved": None}],
    ]
    assert xero.asked == ["con-1"]


def test_user_names_come_in_one_query(client, shop, xero, django_assert_num_queries):
    report = _report(shop, publishing_status="completed", xero_integrated=True)
    for i in range(5):
        person = make_user(f"p{i}@test.com", first_name=f"P{i}")
        _row(report, "published", user=person, at=NOW - timedelta(hours=i))

    history = pr.publish_history(shop.entity.id, report)
    with django_assert_num_queries(1):
        history = pr.publish_history(shop.entity.id, report)

    assert [h["user_name"] for h in history] == ["P0 Owner", "P1 Owner", "P2 Owner", "P3 Owner", "P4 Owner"]


def test_no_attempts_is_an_empty_history(client, shop, xero):
    report = _report(shop)
    assert _get(client, shop, report.id).json() == {"history": []}


def test_refused_like_reading_the_report(client, shop, xero):
    report = _report(shop)
    stranger = make_user("stranger@test.com")
    assert _get(client, shop, report.id, user=stranger).status_code == 403  # not a member: the door

    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    theirs = make_report(make_entity(shop.owner, name="Other Shop"), D3, created_by=shop.owner.id)
    for user, report_id in ((base, report.id), (shop.owner, theirs.id), (shop.owner, uuid.uuid4()),
                            (shop.owner, "nope")):
        resp = _get(client, shop, report_id, user=user)
        assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that report."}
