"""``POST /suppliers`` (``services/suppliers.py``) - Flask's ``tests/test_supplier_create.py`` API
expectations, restated, with the access token from Flask's token service (``core/xero_tokens.py``)
instead of Flask's own credentials. HTTP is faked: the token service and Xero's Contacts."""

from __future__ import annotations

import json

import jwt
import pytest
from django.conf import settings
from minty_db.enums import EntityRole
from minty_db.models import Entity, UserEntity, XeroContactSync

from petty_cash.tests.conftest import headers, make_user, url

pytestmark = pytest.mark.django_db
ORG = "org-1234"
TOKEN_URL = "http://flask.invalid/api/internal/xero/token"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    """Answers the token service and Xero; records what was sent."""

    def __init__(self):
        self.sent = []
        self.token_status = 200
        self.xero_status = 200
        self.contacts = [{"ContactID": "con-new", "Name": "Corner Bakery"}]
        self.found: list[dict] = []  # Xero's answer to a name search
        self.searched: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None, **_):
        assert url == CONTACTS_URL, url
        self.searched.append({"headers": headers or {}, "params": params})
        return _Response(200, {"Contacts": self.found})

    def post(self, url, headers=None, json=None, timeout=None, **_):
        self.sent.append({"url": url, "headers": headers or {}, "json": json})
        if url == TOKEN_URL:
            if self.token_status != 200:
                return _Response(self.token_status, {"status": "reconnect_required"})
            return _Response(200, {"access_token": "xero-access", "xero_org_id": ORG})
        if url == CONTACTS_URL:
            return _Response(self.xero_status, {"Contacts": self.contacts} if self.xero_status == 200 else {})
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.get", fake.get)
    return fake


@pytest.fixture
def connected(shop):
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    return shop


def _post(client, shop, body, user=None):
    return client.post(url(shop.entity.id, "/suppliers"), body, content_type="application/json",
                       headers=headers((user or shop.owner).id))


def _synced(shop) -> dict:
    return dict(XeroContactSync.objects.filter(entity_id=shop.entity.id).values_list("xero_contact_id", "name"))


def test_a_new_supplier_is_created_in_xero_and_kept_as_a_synced_contact(client, connected, http):
    """Added while connected, it is made in both (owner's decisions doc C5, 2026-11-19): Xero is
    searched for the name first, then it is made there as a supplier."""
    resp = _post(client, connected, {"name": " Corner Bakery "})

    assert resp.status_code == 201 and resp.json() == {"id": "con-new", "name": "Corner Bakery"}
    assert [s["params"]["searchTerm"] for s in http.searched] == ["Corner"]  # its longest word (the first of equals)
    token_call, xero_call = http.sent
    assert xero_call["url"] == CONTACTS_URL
    assert xero_call["headers"]["Authorization"] == "Bearer xero-access"
    assert xero_call["headers"]["Xero-Tenant-Id"] == ORG
    assert xero_call["json"] == {"Contacts": [{"Name": "Corner Bakery", "IsCustomer": False, "IsSupplier": True}]}
    assert _synced(connected) == {"con-new": "Corner Bakery"}
    assert XeroContactSync.objects.get(xero_contact_id="con-new").xero_org_id == ORG


def test_the_token_is_asked_of_flask_for_this_company_only(client, connected, http):
    _post(client, connected, {"name": "Corner Bakery"})

    assertion = http.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "xero-access-token" and claims["entity_id"] == str(connected.entity.id)
    assert claims["exp"] - claims["iat"] == 60


def test_an_existing_synced_contact_is_renamed_not_duplicated(client, connected, http):
    XeroContactSync.objects.create(entity_id=connected.entity.id, xero_contact_id="con-new", name="Old name")

    _post(client, connected, {"name": "Corner Bakery"})

    assert _synced(connected) == {"con-new": "Corner Bakery"}


@pytest.mark.parametrize("body, message", [
    ({"name": "  "}, "The supplier's name is still empty. Add the name to continue."),
    ({}, "The supplier's name is still empty. Add the name to continue."),
    ({"name": 7}, "The supplier's name is still empty. Add the name to continue."),
    ({"name": "x" * 151}, "Keep the supplier's name to 150 characters or fewer."),
])
def test_a_bad_name_is_refused_before_calling_xero(client, connected, http, body, message):
    resp = _post(client, connected, body)

    assert resp.status_code == 422 and resp.json()["error"] == message
    assert http.sent == []


def test_xero_refusing_is_said_and_nothing_is_kept(client, connected, http):
    http.xero_status = 500

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json() == {"error": "I couldn't add that contact to Xero. Mind trying again?"}
    assert _synced(connected) == {}


def test_a_contact_without_an_id_is_said(client, connected, http):
    http.contacts = [{"Name": "Corner Bakery"}]

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409 and resp.json()["error"].startswith("Xero didn't tell me which contact")
    assert _synced(connected) == {}


def test_a_company_without_xero_adds_its_own_supplier(client, shop, http):
    """Contacts are never blocked behind Xero (owner, 2026-11-12): the company's own row, no Xero id;
    publish puts it in Xero (owner's decisions doc C1)."""
    resp = _post(client, shop, {"name": "Corner Bakery"})

    assert resp.status_code == 201, resp.json()
    row = XeroContactSync.objects.get(entity_id=shop.entity.id)
    assert (row.name, row.xero_contact_id, row.source) == ("Corner Bakery", None, "user")
    assert http.sent == []


def test_a_disconnected_company_keeping_its_organisation_adds_its_own_too(client, connected, http):
    """A disconnect keeps the organisation (2026-11-10): no token is asked for, Xero waits."""
    Entity.objects.filter(id=connected.entity.id).update(status="disconnected")

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 201
    assert XeroContactSync.objects.get(entity_id=connected.entity.id).xero_contact_id is None
    assert http.sent == []


def test_a_name_the_company_has_is_that_supplier_never_a_second(client, connected, http):
    linked = XeroContactSync.objects.create(entity_id=connected.entity.id, name="Corner  Bakery",
                                            xero_contact_id="con-cb", xero_org_id=ORG)

    resp = _post(client, connected, {"name": "corner bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-cb"
    assert XeroContactSync.objects.filter(entity_id=connected.entity.id).count() == 1
    assert linked.xero_contact_id == "con-cb" and http.sent == []


def test_live_a_supplier_kept_only_in_minty_is_put_in_xero_too_never_a_second(client, connected, http):
    """C5 (2026-11-20, as onboarding-api's chart): added again while connected, the company's own
    supplier is linked to Xero's contact of its name, else made there - the same row, now in Xero."""
    own = XeroContactSync.objects.create(entity_id=connected.entity.id, name="Corner Bakery", source="user")

    resp = _post(client, connected, {"name": "corner  bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-new"
    own.refresh_from_db()
    assert (own.xero_contact_id, own.xero_org_id) == ("con-new", ORG)
    assert XeroContactSync.objects.filter(entity_id=connected.entity.id).count() == 1
    assert [s["params"]["searchTerm"] for s in http.searched] == ["Corner"]  # its longest word (the first of equals)


def test_a_minty_supplier_put_in_xero_takes_xeros_name_and_its_bills_follow(client, connected, http):
    """C5 with Xero's contact named another way: the supplier takes Xero's name at the link and the
    bills on it follow at once (the user, 2026-11-27)."""
    from minty_db.models import Bill

    own = XeroContactSync.objects.create(entity_id=connected.entity.id, name="7 Eleven", source="user")
    bill = Bill.objects.create(entity_id=connected.entity.id, reference="B-7", contact_id=own.id,
                               contact_name="7 Eleven")
    http.found = [{"ContactID": "con-7", "Name": "7-Eleven", "ContactStatus": "ACTIVE"}]

    assert _post(client, connected, {"name": "7 eleven"}).json()["id"] == "con-7"
    own.refresh_from_db()
    assert (own.xero_contact_id, own.name) == ("con-7", "7-Eleven")
    assert Bill.objects.get(id=bill.id).contact_name == "7-Eleven"


def test_a_publish_puts_a_minty_supplier_in_xero_under_xeros_name(connected, monkeypatch):
    """C1 at publish: the supplier made in Minty is linked to Xero's contact of its name - and takes
    that name (the user, 2026-11-27)."""
    from types import SimpleNamespace

    from minty_db.models import Bill

    from petty_cash.xero import contacts

    own = XeroContactSync.objects.create(entity_id=connected.entity.id, name="7 Eleven", source="user")
    bill = Bill.objects.create(entity_id=connected.entity.id, reference="B-7", contact_id=own.id,
                               contact_name="7 Eleven")
    monkeypatch.setattr(contacts, "found_or_made", lambda token, org, name: {"ContactID": "con-7", "Name": "7-Eleven"})
    run = SimpleNamespace(access_token="tok", org=ORG, entity=connected.entity)

    assert contacts.xero_id_for(run, own) == "con-7"
    own.refresh_from_db()
    assert (own.xero_contact_id, own.name) == ("con-7", "7-Eleven")
    assert Bill.objects.get(id=bill.id).contact_name == "7-Eleven"


def test_a_name_typed_another_way_is_that_supplier_never_a_second(client, connected, http):
    """The user, 2026-11-23: case, spaces and punctuation are ignored ("7 Eleven" is "7-Eleven")."""
    linked = XeroContactSync.objects.create(entity_id=connected.entity.id, name="7-Eleven",
                                            xero_contact_id="con-7", xero_org_id=ORG)

    resp = _post(client, connected, {"name": "7 eleven"})

    assert resp.status_code == 201 and resp.json()["id"] == linked.xero_contact_id
    assert XeroContactSync.objects.filter(entity_id=connected.entity.id).count() == 1
    assert http.sent == [] and http.searched == []


def test_xeros_contact_typed_another_way_is_found_and_linked_never_a_twin(client, connected, http):
    """Xero's search is a substring search, so it is asked for the name's longest word and the
    answer's names are compared as Minty compares them."""
    http.found = [{"ContactID": "con-7c", "Name": "7-Eleven Central", "ContactStatus": "ACTIVE"},
                  {"ContactID": "con-7", "Name": "7-ELEVEN", "ContactStatus": "ACTIVE"}]

    resp = _post(client, connected, {"name": "7 Eleven"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-7"
    # Xero's name at the link (the user, 2026-11-27), never a silent flip at the next sync
    assert XeroContactSync.objects.get(entity_id=connected.entity.id, xero_contact_id="con-7").name == "7-ELEVEN"
    assert [s["params"]["searchTerm"] for s in http.searched] == ["Eleven"]
    assert [s["url"] for s in http.sent] == [TOKEN_URL]  # nothing made in Xero


def test_the_lists_hold_the_current_organisation_s_contacts_and_minty_s_own(connected):
    """As Payment Request (the user, 2026-11-23): a company's contacts from an organisation it was
    connected to before are off its pickers, its reader and its line checks; the contacts with no
    organisation recorded (made in Minty, or older rows) stay."""
    from petty_cash.services.expenses import find_supplier, public_id, suppliers

    entity_id = connected.entity.id
    mine = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id="con-now", xero_org_id=ORG,
                                          name="Now Ltd")
    own = XeroContactSync.objects.create(entity_id=entity_id, name="Own Ltd", source="user")
    old = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id="con-old", xero_org_id="org-old",
                                         name="Old Ltd")

    assert set(suppliers(entity_id)) == {mine, own}
    assert find_supplier(entity_id, public_id(old)) is None
    # no organisation at all: nothing to scope against, every contact is listed
    Entity.objects.filter(id=entity_id).update(xero_org_id=None, status="onboarding")
    assert set(suppliers(entity_id)) == {mine, own, old}


def test_a_name_only_another_organisation_holds_is_a_new_supplier_here(client, connected, http):
    """Never the old organisation's contact - it is on no list here and Xero would not know it."""
    old = XeroContactSync.objects.create(entity_id=connected.entity.id, xero_contact_id="con-old",
                                         xero_org_id="org-old", name="7-Eleven")

    resp = _post(client, connected, {"name": "7 Eleven"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-new"
    old.refresh_from_db()
    assert (old.xero_contact_id, old.xero_org_id, old.record_status) == ("con-old", "org-old", "active")


def test_a_deleted_contact_of_another_organisation_is_never_brought_back(client, connected, http):
    gone = XeroContactSync.objects.create(entity_id=connected.entity.id, xero_contact_id="con-old",
                                          xero_org_id="org-old", name="Corner Bakery", record_status="archived")

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-new"
    gone.refresh_from_db()
    assert gone.record_status == "archived"


def test_offline_a_supplier_kept_only_in_minty_stays_so(client, shop, http):
    own = XeroContactSync.objects.create(entity_id=shop.entity.id, name="Corner Bakery", source="user")

    resp = _post(client, shop, {"name": "corner bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == str(own.id)
    assert http.sent == [] and http.searched == []


def test_a_live_company_links_to_xeros_contact_of_that_name_before_making_one(client, connected, http):
    from minty_db.models import XeroContact

    XeroContact.objects.create(entity_id=connected.entity.id, xero_org_id=ORG, xero_guid="con-kept",
                               name="Corner Bakery", status="ACTIVE")

    resp = _post(client, connected, {"name": "CORNER BAKERY"})

    assert resp.status_code == 201
    assert _synced(connected) == {"con-kept": "Corner Bakery"}  # Xero's name at the link (2026-11-27)
    assert http.sent == []  # nothing asked of Xero: no duplicate there


def test_a_live_company_links_to_the_contact_xero_has_of_that_name_never_a_twin(client, connected, http):
    """C5: Xero searched by name (its search is loose, so only the exact name, case and extra spaces
    ignored, counts); the one it has is linked and nothing is made."""
    http.found = [{"ContactID": "con-cbk", "Name": "Corner Bakery Kowloon", "ContactStatus": "ACTIVE"},
                  {"ContactID": "con-cb", "Name": "CORNER  BAKERY", "ContactStatus": "ACTIVE"}]

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-cb"
    assert _synced(connected) == {"con-cb": "CORNER  BAKERY"}  # Xero's name at the link (2026-11-27)
    assert [s["url"] for s in http.sent] == [TOKEN_URL]  # the token only: nothing made in Xero


def test_xero_not_answering_the_search_is_said_and_nothing_is_made(client, connected, http, monkeypatch):
    import requests

    def down(*_a, **_k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("requests.get", down)
    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json() == {"error": "I couldn't add that contact to Xero. Mind trying again?"}
    assert [s["url"] for s in http.sent] == [TOKEN_URL] and _synced(connected) == {}


def test_a_connection_needing_a_reconnect_is_said(client, connected, http):
    http.token_status = 409

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json()["error"] == "Xero wouldn't let me in. Could you ask an admin to reconnect it?"
    assert [s["url"] for s in http.sent] == [TOKEN_URL]


def test_a_token_service_failure_is_a_503(client, connected, http):
    http.token_status = 500

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 503 and _synced(connected) == {}


def test_xero_refusing_the_token_asks_for_a_reconnect(client, connected, http):
    http.xero_status = 401

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409 and resp.json()["error"].startswith("Xero wouldn't let me in")


def test_a_member_without_the_right_is_refused(client, connected, http):
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=connected.entity.id, role=EntityRole.ENTITY_BASE,
                              approved=True)

    resp = _post(client, connected, {"name": "Corner Bakery"}, user=base)

    assert resp.status_code == 403 and http.sent == []


def test_a_sync_storing_the_same_contact_first_is_not_an_error(client, connected, http, monkeypatch):
    """2026-11-03: one row per Xero contact per company (``uq_xcs_entity_contact``). Flask's
    sync can store the new contact between the look-up and the insert; the insert then meets
    the key, and the row the sync wrote is the supplier - renamed, never a second row or a 500."""
    from django.db import IntegrityError

    XeroContactSync.objects.create(entity_id=connected.entity.id, xero_contact_id="con-new",
                                   xero_org_id=ORG, name="From the sync")
    manager = XeroContactSync.objects
    real_filter, looked = manager.filter, []

    def before_the_sync(*args, **kwargs):  # the look-up ran before the sync committed
        found = real_filter(*args, **kwargs)
        if not looked:
            looked.append(True)
            return found.none()
        return found

    def meets_the_key(**_values):
        raise IntegrityError('duplicate key value violates unique constraint "uq_xcs_entity_contact"')

    monkeypatch.setattr(manager, "filter", before_the_sync)
    monkeypatch.setattr(manager, "create", meets_the_key)
    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 201, resp.content
    assert resp.json() == {"id": "con-new", "name": "Corner Bakery"}
    monkeypatch.undo()
    assert _synced(connected) == {"con-new": "Corner Bakery"}


# ---- a deleted supplier added again is that one, brought back (owner, 2026-11-12) ---------------------


def _archived(shop, name, xero_id=None):
    return XeroContactSync.objects.create(entity_id=shop.entity.id, name=name, xero_contact_id=xero_id,
                                          xero_org_id=ORG if xero_id else None, record_status="archived")


def test_a_deleted_supplier_comes_back_in_minty_and_nothing_is_restored_in_xero(client, connected, http):
    """Minty never restores anything in the customer's Xero (owner's decisions doc C4, 2026-11-19):
    the row comes back in Minty; if Xero still has it archived, publish asks."""
    gone = _archived(connected, "Corner Bakery", "con-old")

    resp = _post(client, connected, {"name": "corner bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-old"
    assert XeroContactSync.objects.get(id=gone.id).record_status == "active"
    assert [s for s in http.sent if s["url"] == CONTACTS_URL] == []


def test_xeros_archived_contact_of_that_name_is_linked_as_it_stands_never_a_twin(client, connected, http):
    from minty_db.models import XeroContact

    XeroContact.objects.create(entity_id=connected.entity.id, xero_org_id=ORG, xero_guid="con-old",
                               name="Corner Bakery", status="ARCHIVED")

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 201 and resp.json()["id"] == "con-old"
    assert [s for s in http.sent if s["url"] == CONTACTS_URL] == []  # not restored (C4)
    assert XeroContact.objects.get(xero_guid="con-old").status == "ARCHIVED"


def test_offline_a_deleted_supplier_comes_back_too_with_no_xero_call(client, shop, http):
    own = _archived(shop, "Corner Bakery")
    sam = _archived(shop, "Sam's Fish", "con-sam")

    back = _post(client, shop, {"name": "Corner Bakery"})
    assert back.status_code == 201 and back.json()["id"] == str(own.id)
    again = _post(client, shop, {"name": "Sam's Fish"})
    assert again.status_code == 201 and again.json()["id"] == "con-sam"
    assert XeroContactSync.objects.get(id=sam.id).record_status == "active"
    assert http.sent == []
