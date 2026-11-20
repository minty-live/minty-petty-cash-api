"""Petty Cash settings - Flask's ``petty_cash_api/routes/settings.py``: the Electronic / Delivery
sales methods here; the Xero cards forwarded to Flask. The country and currency are not set here
(owner, 2026-11-03): minty-web's General tab is their one place (2026-11-10).

- ``GET /settings``: the two method lists, the catalogue methods not on them, whether Xero is
  connected, and what this person may change.
- ``POST /sales-methods`` ``{type, sale_info_id | name}``, ``PUT /sales-methods/{id}`` ``{name}``,
  ``DELETE /sales-methods/{id}``, ``PUT /sales-methods/order`` ``{type, method_ids}``.
- ``PUT /settings/sales`` ``{accounts: {cash, electronic, delivery}, methods: {electronic: [..],
  delivery: [..]}}``: the hub's "Sales accounts and methods" card in one save (2026-11-20).
- ``GET /settings/xero``, ``PUT /settings/xero/mapping``, ``PUT /settings/xero/account-codes``: the
  Xero cards (``services/xero_cards.py``; done here since Phase 3 of the Flask retirement,
  2026-11-06 - forwarded to Flask until then). ``COA_VIEW`` to read, ``XERO_RIGHTS`` to save (403).

Every write answers the whole settings payload with a ``message``. Name lengths are the schema's -
``sale_info.sale_name`` and ``value_name`` are 80 characters, and a new method's ``value_name`` is
its name + ``_sales`` - where Flask's 100-character check let a 75-100 character name through to
a database error.
"""

from __future__ import annotations

import uuid

from django.http import JsonResponse
from minty_db.models import Entity, SaleInfo
from ninja import Router

from core.exceptions import Refused
from core.policy import Permission, has_permission
from petty_cash.routes._input import json_body
from petty_cash.services import xero_cards
from petty_cash.services.sales import sales_groups, sales_parts, set_sales_parts
from petty_cash.services.settings import (
    NOT_FOUND,
    PICK_OFFERED,
    TYPES,
    SettingsRefused,
    add_method,
    available,
    methods,
    offered,
    remove_method,
    rename_method,
    reorder_methods,
    save_sales,
)

router = Router()

NO_VIEW = "You don't have permission to see these settings."

#: the most a NEW method's name may hold: its value_name (name + "_sales") must fit 80 characters
ADD_NAME_MAX = 80 - len("_sales")
#: a rename changes sale_name only
RENAME_NAME_MAX = 80
#: the Xero cards' saves, only with all four
XERO_RIGHTS = ("ENTITY_UPDATE", "COA_UPDATE", "COA_CREATE", "COA_DELETE")
NO_RIGHT = "You don't have permission to change these settings. Ask an Accountant or an Admin."


def _may(request, *names) -> bool:
    return all(has_permission(request.auth_user, getattr(Permission, n), request.company.id) for n in names)


def _body(request) -> dict:
    data = json_body(request)
    return data if isinstance(data, dict) else {}


def _uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _name(raw, limit: int, verb: str) -> str:
    name = str(raw or "").strip()
    if not name:
        raise Refused(f"The method's name is still empty. Type a name to {verb} it.", 422)
    if len(name) > limit:
        raise Refused(f"That name is too long. Keep it to {limit} characters or fewer.", 422)
    return name


def settings_payload(request) -> dict:
    entity_id = request.company.id
    org = Entity.objects.get(id=entity_id)
    may_view_sales = _may(request, "SALES_METHOD_VIEW")
    return {
        "sales_methods": methods(entity_id) if may_view_sales else {t: [] for t in TYPES},
        "available": available(entity_id) if may_view_sales else {t: [] for t in TYPES},
        # one Non-cash card, or Electronic and Delivery (owner, 2026-11-11)
        "sales_groups": sales_groups(entity_id),
        # the three sales accounts on or off (owner, 2026-11-17): which parts the daily report asks for
        "sales_parts": sales_parts(entity_id),
        # a disconnect keeps the organisation (Xero optional, 2026-11-10): the status says connected
        "xero_connected": bool(org.xero_org_id) and org.status != "disconnected",
        "permissions": {
            "edit_xero": _may(request, *XERO_RIGHTS),
            "view_sales_methods": may_view_sales,
            "add_sales_method": _may(request, "SALES_METHOD_CREATE"),
            "rename_sales_method": _may(request, "SALES_METHOD_UPDATE"),
            "remove_sales_method": _may(request, "SALES_METHOD_DELETE"),
            "reorder_sales_methods": _may(request, "SALES_METHOD_REORDER"),
            "edit_sales_accounts": _may(request, "SALES_METHOD_UPDATE"),
        },
    }


def _answer(request, message: str, status: int = 200):
    return JsonResponse({**settings_payload(request), "message": message}, status=status)


def _listed(entity_id, method_id) -> bool:
    """One of the company's Electronic / Delivery methods - Cash is managed on the report."""
    return any(m["id"] == str(method_id) for group in methods(entity_id).values() for m in group)


@router.get("/settings")
def settings_page(request):
    if not _may(request, "COA_VIEW"):
        raise Refused(NO_VIEW, 403)
    return settings_payload(request)


@router.get("/settings/xero")
def xero_settings(request):
    if not _may(request, "COA_VIEW"):
        raise Refused(NO_VIEW, 403)
    return _xero(request, "read", None)


@router.put("/settings/xero/mapping")
def save_xero_mapping(request):
    """``{main_bank, deposit_bank, cashsale_account, cashsale_contact, owners_account,
    owners_contact, discrepancy_account, discrepancy_contact}`` - Xero ids from the read's options."""
    if not _may(request, *XERO_RIGHTS):
        raise Refused(NO_RIGHT, 403)
    return _xero(request, "mapping", _body(request))


@router.put("/settings/xero/account-codes")
def save_xero_account_codes(request):
    """``{codes: [..]}``: exactly these account codes ticked for Petty Cash (at least one)."""
    if not _may(request, *XERO_RIGHTS):
        raise Refused(NO_RIGHT, 403)
    return _xero(request, "account-codes", _body(request))


def _xero(request, action: str, body):
    answer, status = xero_cards.answer(request.company.id, action, body, may_edit=_may(request, *XERO_RIGHTS))
    return JsonResponse(answer, status=status)


NO_SALES_ACCOUNT = "No sales account is ticked yet. Tick at least one to continue."


@router.put("/settings/sales-accounts")
def save_sales_accounts(request):
    """``{cash, electronic, delivery}`` - Sales - Cash, Electronic Payment and Delivery on or off
    (owner, 2026-11-17; Figma 464:73 row 4). At least one stays on. Turning one off keeps its methods:
    the daily report stops asking for that part, and they come back when it is turned on again."""
    if not _may(request, "SALES_METHOD_UPDATE"):
        raise Refused(NO_RIGHT, 403)
    set_sales_parts(request.company.id, *_ticks(_body(request)))
    return _answer(request, "Your sales accounts are saved.")


def _ticks(data) -> tuple[bool, bool, bool]:
    ticks = [data.get(k) for k in ("cash", "electronic", "delivery")] if isinstance(data, dict) else [None]
    if not all(isinstance(t, bool) for t in ticks):
        raise Refused("Turn each sales account on or off to continue.", 422)
    if not any(ticks):
        raise Refused(NO_SALES_ACCOUNT, 422)
    return tuple(ticks)


NO_METHOD_LISTS = "Send each type's methods in their order to save them."


def _wanted_item(item) -> dict:
    """One method of a list, checked: ``{id, name?}`` kept (and renamed), ``{sale_info_id}`` or
    ``{name}`` added."""
    if not isinstance(item, dict):
        raise Refused(NO_METHOD_LISTS, 422)
    if item.get("id"):
        if not _uuid(item["id"]):
            raise Refused(NOT_FOUND, 404)
        name = item.get("name")
        return {"id": str(uuid.UUID(str(item["id"]))),
                "name": None if name is None else _name(name, RENAME_NAME_MAX, "save")}
    if item.get("sale_info_id"):
        if not _uuid(item["sale_info_id"]):
            raise Refused(PICK_OFFERED, 422)
        return {"sale_info_id": str(item["sale_info_id"])}
    return {"name": _name(item.get("name"), ADD_NAME_MAX, "add")}


@router.put("/settings/sales")
def save_sales_settings(request):
    """The hub's "Sales accounts and methods" card, held until Save changes (2026-11-20):
    ``{accounts: {cash, electronic, delivery}, methods: {electronic: [..], delivery: [..]}}``, each
    list in its new order of ``{id, name?}`` (kept; a different name renames), ``{sale_info_id}`` or
    ``{name}`` (added); a method left out of its list is removed, a type left out stays as it is.
    The per-change rules and sentences hold, in one transaction: any refusal writes nothing."""
    if not _may(request, "SALES_METHOD_UPDATE"):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    ticks = _ticks(data.get("accounts"))
    lists = data.get("methods")
    if not isinstance(lists, dict) or any(k not in TYPES or not isinstance(v, list) for k, v in lists.items()):
        raise Refused(NO_METHOD_LISTS, 422)
    wanted = {kind: [_wanted_item(item) for item in items] for kind, items in lists.items()}
    for items in wanted.values():
        ids = [item["id"] for item in items if item.get("id")]
        if len(ids) != len(set(ids)):
            raise Refused(NO_METHOD_LISTS, 422)

    def allow(right: str) -> None:
        if not _may(request, right):
            raise Refused(NO_RIGHT, 403)

    try:
        save_sales(request.company.id, ticks, wanted, allow=allow)
    except SettingsRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return _answer(request, "Your sales accounts and methods are saved.")


@router.post("/sales-methods")
def add_sales_method(request):
    if not _may(request, "SALES_METHOD_CREATE"):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    kind = data.get("type")
    if kind not in TYPES:
        raise Refused("Choose Electronic or Delivery for the method.", 422)
    entity_id = request.company.id
    sale_info_id = None
    if data.get("sale_info_id"):
        sale = (
            SaleInfo.objects.filter(offered(entity_id), id=str(data["sale_info_id"])).first()
            if _uuid(data["sale_info_id"]) else None
        )
        if sale is None or not sale.enabled or str(sale.type) != kind:
            raise Refused(PICK_OFFERED, 422)
        sale_info_id, name = str(sale.id), sale.sale_name
    else:
        name = _name(data.get("name"), ADD_NAME_MAX, "add")
    if any(m["name"].casefold() == name.casefold() for m in methods(entity_id)[kind]):
        raise Refused(f"{name} is already on the list.", 409)
    add_method(entity_id, kind, sale_info_id=sale_info_id, name=name)
    return _answer(request, f"{name} added.", 201)


@router.put("/sales-methods/order")
def reorder_sales_methods(request):
    if not _may(request, "SALES_METHOD_REORDER"):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    kind, ids = data.get("type"), data.get("method_ids")
    if kind not in TYPES or not isinstance(ids, list):
        raise Refused("Send the type and the methods in their new order.", 422)
    entity_id = request.company.id
    current = [m["id"] for m in methods(entity_id)[kind]]
    if sorted(str(i) for i in ids) != sorted(current):
        raise Refused("The list changed since it was loaded. Reload the page and order it again.", 409)
    # one type at a time, as the page's cards save it: the other type keeps its own order
    reorder_methods(entity_id, [str(i) for i in ids])
    return _answer(request, "Order saved.")


@router.put("/sales-methods/{method_id}")
def rename_sales_method(request, method_id: str):
    if not _may(request, "SALES_METHOD_UPDATE"):
        raise Refused(NO_RIGHT, 403)
    entity_id = request.company.id
    if not _uuid(method_id) or not _listed(entity_id, method_id):
        raise Refused(NOT_FOUND, 404)
    name = _name(_body(request).get("name"), RENAME_NAME_MAX, "save")
    try:
        rename_method(entity_id, str(uuid.UUID(method_id)), name)
    except SettingsRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return _answer(request, "Name saved.")


@router.delete("/sales-methods/{method_id}")
def remove_sales_method(request, method_id: str):
    if not _may(request, "SALES_METHOD_DELETE"):
        raise Refused(NO_RIGHT, 403)
    entity_id = request.company.id
    if not _uuid(method_id) or not _listed(entity_id, method_id):
        raise Refused(NOT_FOUND, 404)
    return _answer(request, remove_method(entity_id, str(uuid.UUID(method_id))))
