"""The contacts a publish names, and the one way Minty finds or makes a contact in Xero.

The owner's decisions doc (2026-11-19): Minty writes a contact to Xero at two moments only - when a
supplier is added while Xero is connected (C5, ``services/suppliers.py``) and at publish, for a
contact the record uses that Xero doesn't have yet (C1). Either way Xero is first searched for the
name (case and extra spaces ignored); found, that ContactID is stored on the Minty contact - never
a duplicate in Xero; not found, it is made there as a supplier and its id stored. The id always goes
on the same Minty row (``xero_contact_sync``): no second row for one contact.

A contact Xero lost since it was linked (404 on its id) is found or made again the same way.
Phase 3 of the Flask retirement (2026-11-05) moved these calls here from Flask's ``publish.py``.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from minty_db.models import XeroContactSync

logger = logging.getLogger("petty-cash-api")

#: Flask's for both contact calls.
TIMEOUT = 10


class XeroContactUnavailable(Exception):
    """Xero couldn't be asked about a contact, or refused making one (logged where raised).
    ``status``: Xero's HTTP status when it answered; ``no_id``: it made one but named no id."""

    def __init__(self, status: int | None = None, no_id: bool = False):
        super().__init__(status, no_id)
        self.status, self.no_id = status, no_id


def name_key(name) -> str:
    """A contact's name as Minty compares it: case and extra spaces ignored."""
    return " ".join(str(name or "").split()).lower()


def _headers(access_token: str, org) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Xero-Tenant-Id": str(org), "Accept": "application/json",
            "Content-Type": "application/json"}


def find_by_name(access_token: str, org, name: str) -> dict | None:
    """Xero's active contact named ``name`` (case and extra spaces ignored), or None. Xero's
    ``searchTerm`` is a loose case-insensitive search (Name, FirstName, LastName, ContactNumber,
    EmailAddress - the accounting API spec's ``getContacts``), so only an exact name counts.
    Raises ``XeroContactUnavailable`` when Xero can't be asked."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts", headers=_headers(access_token, org),
                                params={"searchTerm": " ".join(str(name).split()), "page": 1}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Xero unreachable looking for contact %r: %s", name, exc)
        raise XeroContactUnavailable from None
    if response.status_code != 200:
        logger.error("Xero answered %s looking for contact %r: %s", response.status_code, name,
                     (response.text or "")[:300])
        raise XeroContactUnavailable(response.status_code)
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        logger.error("Xero answered no JSON looking for contact %r", name)
        raise XeroContactUnavailable from None
    key = name_key(name)
    return next((c for c in contacts if c.get("ContactID") and name_key(c.get("Name")) == key
                 and str(c.get("ContactStatus") or "ACTIVE").upper() == "ACTIVE"), None)


def make_supplier(access_token: str, org, name: str) -> dict:
    """``name`` made in Xero as a supplier; Xero's contact. Raises ``XeroContactUnavailable``."""
    try:
        response = requests.post(f"{settings.XERO_API_BASE_URL}/Contacts", headers=_headers(access_token, org),
                                 json={"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]},
                                 timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Xero unreachable making contact %r: %s", name, exc)
        raise XeroContactUnavailable from None
    if response.status_code not in (200, 201):
        logger.error("Xero refused contact %r: %s %s", name, response.status_code, (response.text or "")[:300])
        raise XeroContactUnavailable(response.status_code)
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        contacts = []
    if not contacts or not contacts[0].get("ContactID"):
        logger.error("Xero made contact %r but gave no ContactID", name)
        raise XeroContactUnavailable(response.status_code, no_id=True)
    logger.info("Contact %s made in Xero for %r", contacts[0]["ContactID"], name)
    return contacts[0]


def found_or_made(access_token: str, org, name: str) -> dict:
    """Xero's contact of ``name``, else a new supplier there. Raises ``XeroContactUnavailable``."""
    return find_by_name(access_token, org, name) or make_supplier(access_token, org, name)


def _store(run, contact: XeroContactSync, guid: str) -> str:
    """``guid`` on this Minty contact. Another of the company's contacts already holding it (one
    Xero contact, one row): the publish names it, and this row is left as it is (logged)."""
    holder = (XeroContactSync.objects.filter(entity_id=run.entity.id, xero_contact_id=guid)
              .exclude(id=contact.id).first())
    if holder is not None:
        logger.warning("Publish: Xero's contact %s for %r is already Minty contact %s; posted with it", guid,
                       contact.name, holder.id)
        return guid
    XeroContactSync.objects.filter(id=contact.id).update(xero_contact_id=guid, xero_org_id=str(run.org))
    contact.xero_contact_id = guid
    return guid


def xero_id_for(run, contact: XeroContactSync) -> str | None:
    """The ContactID a publish names for ``contact`` (C1): its own when linked, else Xero's of its
    name or a new supplier there - stored on this row. None when Xero can't be asked (logged): the
    entry fails rather than go without its contact."""
    if contact.xero_contact_id:
        return contact.xero_contact_id
    try:
        made = found_or_made(run.access_token, run.org, contact.name)
    except XeroContactUnavailable:
        return None
    return _store(run, contact, made["ContactID"])


def ensure_contact(run, contact_id: str, name: str | None) -> str | None:
    """A ContactID Xero knows: ``contact_id`` when Xero has it; when Xero lost it (404), Xero's
    contact of ``name`` or a new one - its id put on this company's Minty contacts that held the old
    one. None when Xero can't be asked (logged): the entry then fails."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts/{contact_id}",
                                headers=_headers(run.access_token, run.org), timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Publish: Xero unreachable checking contact %s: %s", contact_id, exc)
        return None
    if response.status_code == 200:
        return contact_id
    if response.status_code != 404:
        logger.error("Publish: checking contact %s answered %s", contact_id, response.status_code)
        return None
    logger.warning("Publish: contact %s is gone from Xero; finding or making %r again", contact_id, name)
    if not name:
        return None
    try:
        new_id = found_or_made(run.access_token, run.org, name)["ContactID"]
    except XeroContactUnavailable:
        return None
    if XeroContactSync.objects.filter(entity_id=run.entity.id, xero_contact_id=new_id).exists():
        logger.warning("Publish: Xero's contact %s for %r is already a Minty contact; posted with it", new_id, name)
        return new_id
    moved = XeroContactSync.objects.filter(entity_id=run.entity.id, xero_contact_id=contact_id).update(
        xero_contact_id=new_id, xero_org_id=str(run.org))
    logger.info("Publish: %s Minty contact(s) moved from Xero contact %s to %s", moved, contact_id, new_id)
    return new_id


# ---- a contact Xero archived (owner's decisions doc C4, 2026-11-19) ---------------------------------

#: accounts-api's relink - its own scope (``xero/views.internal_contact_relink``).
RELINK_SCOPE = "xero-contact-relink"
RELINK_TIMEOUT = (5, 30)
RELINK_UNREACHABLE = "I couldn't reach the contacts just now. Mind trying again in a moment?"


class RelinkRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message, self.status = message, status


def in_xero(access_token: str, org, guid: str) -> dict | None:
    """Xero's contact ``guid`` (``ContactStatus``, ``MergedToContactID`` - the spec returns the merge
    target when a contact is fetched by its id), or None when Xero no longer has it (404). Raises
    ``XeroContactUnavailable``."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts/{guid}", headers=_headers(access_token, org),
                                timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Xero unreachable checking contact %s: %s", guid, exc)
        raise XeroContactUnavailable from None
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.error("Xero answered %s checking contact %s", response.status_code, guid)
        raise XeroContactUnavailable(response.status_code)
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        raise XeroContactUnavailable from None
    return contacts[0] if contacts else None


def relink(entity_id, contact_id, guid: str, reason: str, user_id=None) -> dict:
    """The Minty contact pointed at Xero contact ``guid`` - merged into the Minty contact already
    holding it, if any - through accounts-api, the one implementation. Raises ``RelinkRefused``."""
    from core.accounts_api import assertion_headers, service_assertion

    claims = {"user_id": str(user_id)} if user_id else {}
    try:
        response = requests.post(f"{settings.ACCOUNTS_API_URL}/api/internal/xero/contacts/{contact_id}/relink",
                                 headers=assertion_headers(service_assertion(RELINK_SCOPE, entity_id, **claims)),
                                 json={"xero_contact_id": guid, "reason": reason}, timeout=RELINK_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Contact relink: accounts-api unreachable for contact %s: %s", contact_id, exc)
        raise RelinkRefused(RELINK_UNREACHABLE, 503) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        logger.error("Contact relink: accounts-api answered %s for contact %s", response.status_code, contact_id)
        if response.status_code in (400, 404, 409) and body.get("error"):
            raise RelinkRefused(body["error"], response.status_code)
        raise RelinkRefused(RELINK_UNREACHABLE, 503)
    return body
