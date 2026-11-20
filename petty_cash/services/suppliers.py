"""Adding a supplier: a new contact in the company's Xero organisation AND its synced row
(``xero_contact_sync``) - Flask's ``blueprints/entity/services/suppliers.py``, ported. The access
token comes from Flask (``core/xero_tokens.py``); Flask retried once after its own refresh on a
401/403, which is the token service's job here, so a refusal from Xero is final.

Never blocked behind Xero, never a duplicate (owner, 2026-11-12): a name the company already has
(case and inner spaces ignored) is that supplier - live on Xero, one kept only in Minty is put in
Xero too (owner's decisions doc C5, 2026-11-19); with no live connection the supplier is the
company's own row (no Xero id - publish puts it in Xero, C1); live, Xero's contact of that name is
linked from the stored copies, else searched in Xero, before one is made there. A supplier the
company deleted is that one brought back (onboarding-api's chart rule): its archived row active
again, or Xero's archived contact of the name linked as it stands - Minty never restores anything
in Xero (C4); offline, one still linked to Xero waits for a reconnect.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from minty_db.models import Entity, XeroContact, XeroContactSync

from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.services.xero_settings import minty_on_its_own
from petty_cash.xero.contacts import XeroContactUnavailable, found_or_made

logger = logging.getLogger("petty-cash-api")

NOT_CONNECTED = "This entity isn't connected to Xero yet. Could you ask an admin to connect it?"
NO_ENTRY = "Xero wouldn't let me in. Could you ask an admin to reconnect it?"
NOT_ADDED = "I couldn't add that contact to Xero. Mind trying again?"
NO_ID = "Xero didn't tell me which contact it created. Mind trying again?"
NO_TOKEN_SERVICE = "I couldn't reach Xero just now. Mind trying again in a moment?"
#: ``xero_contact_sync.name``
MAX_NAME = 150


class SupplierRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


def _key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _held(entity_id, name: str) -> XeroContactSync | None:
    """The company's active supplier of that name (case and inner spaces ignored), if any."""
    return next((c for c in XeroContactSync.objects.filter(entity_id=entity_id, record_status="active",
                                                           merged_into__isnull=True)
                 if _key(c.name) == _key(name)), None)


def _kept_in_minty(entity_id, name: str) -> XeroContactSync:
    """No live connection (owner, 2026-11-12; was: only a company keeping its books in Minty): the
    supplier is the company's own row, no Xero id."""
    row = XeroContactSync.objects.create(entity_id=entity_id, name=" ".join(name.split())[:MAX_NAME],
                                         source="user")
    logger.info("Supplier %s kept in Minty for company %s", row.id, entity_id)
    return row


def _access_token(entity_id) -> str:
    try:
        return xero_token(entity_id).access_token
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            raise SupplierRefused(NO_ENTRY) from None
        raise SupplierRefused(NO_TOKEN_SERVICE, 503) from None


def _brought_back(entity: Entity, name: str, offline: bool) -> XeroContactSync | None:
    """The supplier the company deleted with this name, brought back; None when there is none."""
    gone = sorted((c for c in XeroContactSync.objects.filter(entity_id=entity.id, record_status="archived",
                                                             merged_into__isnull=True) if _key(c.name) == _key(name)),
                  key=lambda c: not c.xero_contact_id)
    if gone:
        row = gone[0]
        # Minty never restores anything in the customer's Xero (owner's decisions doc C4,
        # 2026-11-19): the row comes back here; if Xero still has it archived, publish asks
        XeroContactSync.objects.filter(id=row.id).update(record_status="active")
        row.record_status = "active"
        logger.info("Supplier %s brought back for company %s", row.id, entity.id)
        return row
    if offline:
        return None
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id))
                 .exclude(status="ACTIVE") if _key(c.name) == _key(name) and c.xero_guid not in held), None)
    if copy is None:
        return None
    # linked as it stands, not restored (C4): publish asks about an archived one
    row = XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id=copy.xero_guid,
                                         xero_org_id=entity.xero_org_id, name=copy.name[:MAX_NAME],
                                         xero_contact_link_id=copy.id)
    logger.info("Supplier %s brought back from Xero's archive for company %s", row.id, entity.id)
    return row


def _found_or_made(entity: Entity, name: str) -> str:
    """The ContactID of Xero's contact of ``name`` (searched live, case and extra spaces ignored),
    else of one made there as a supplier (C5). Raises ``SupplierRefused``."""
    token = _access_token(entity.id)
    try:
        made = found_or_made(token, entity.xero_org_id, " ".join(name.split()))
    except XeroContactUnavailable as unavailable:
        if unavailable.status in (401, 403):
            raise SupplierRefused(NO_ENTRY) from None
        raise SupplierRefused(NO_ID if unavailable.no_id else NOT_ADDED) from None
    return made["ContactID"]


def _put_in_xero(entity: Entity, row: XeroContactSync) -> XeroContactSync:
    """The company's own supplier, kept only in Minty, added again while live (C5): linked to Xero's
    contact of its name, else made there. If another of the company's rows already holds that Xero
    contact, that row is the supplier (never a second link to one contact)."""
    xero_contact_id = _found_or_made(entity, row.name)
    holder = XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id=xero_contact_id).first()
    if holder is not None:
        return holder
    XeroContactSync.objects.filter(id=row.id).update(xero_contact_id=xero_contact_id, xero_org_id=entity.xero_org_id)
    row.xero_contact_id, row.xero_org_id = xero_contact_id, entity.xero_org_id
    logger.info("Supplier %s kept in Minty is now in Xero for company %s", row.id, entity.id)
    return row


def create_supplier(entity_id, name: str) -> XeroContactSync:
    """The company's supplier of ``name``: the one it has, else - live on Xero - Xero's of that
    name or a new one made there, else its own Minty row."""
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise SupplierRefused(NOT_CONNECTED)
    # offline: no organisation; disconnected (a disconnect keeps the organisation, 2026-11-10); or
    # still keeping its books in Minty - mid-matching too, whose Finish links its suppliers
    offline = not entity.xero_org_id or entity.status == "disconnected" or minty_on_its_own(entity_id)
    held = _held(entity_id, name)
    if held is not None:
        if held.xero_contact_id or offline:
            return held
        return _put_in_xero(entity, held)
    back = _brought_back(entity, name, offline)
    if back is not None:
        return back
    if offline:
        return _kept_in_minty(entity_id, name)
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(entity.xero_org_id),
                                                       status="ACTIVE")
                 if _key(c.name) == _key(name)
                 and not XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=c.xero_guid).exists()),
                None)
    if copy is not None:
        row = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=copy.xero_guid,
                                             xero_org_id=entity.xero_org_id, name=" ".join(name.split())[:MAX_NAME],
                                             xero_contact_link_id=copy.id)
        logger.info("Supplier %s linked to Xero's contact of its name for company %s", row.id, entity_id)
        return row
    # Added while connected, it is made in both (owner's decisions doc C5, 2026-11-19): Xero's contact
    # of the name (searched live, case and extra spaces ignored) is linked, else it is made there as a
    # supplier - never a twin in Xero.
    xero_contact_id = _found_or_made(entity, name)
    created_name = " ".join(name.split())[:MAX_NAME]

    with transaction.atomic():
        row = XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=xero_contact_id).first()
        if row is None:
            try:
                with transaction.atomic():
                    row = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=xero_contact_id,
                                                         xero_org_id=entity.xero_org_id, name=created_name)
            except IntegrityError:
                # Flask's sync stored the same contact since the look-up (uq_xcs_entity_contact,
                # 2026-11-03): that row is this supplier.
                row = XeroContactSync.objects.get(entity_id=entity_id, xero_contact_id=xero_contact_id)
        if row.name != created_name:
            row.name = created_name
            row.save(update_fields=["name"])
    logger.info("Supplier %s added for company %s", xero_contact_id, entity_id)
    return row
