"""Petty Cash settings (``petty_cash/routes/settings.py``) - Flask's
``tests/test_petty_cash_api_settings.py`` expectations, restated, plus what Flask's suite doesn't
pin: the name limits the schema allows (a deliberate difference - Flask's 100-character check let a
75-100 character name through to a database error) and the rename refusals. The country and currency
are not set here: minty-web's General tab is their one place."""

from __future__ import annotations

import pytest
from minty_db.enums import EntityRole, SaleType
from minty_db.models import (
    CountryInfo,
    Entity,
    EntitySaleSetting,
    Report,
    ReportSale,
    SaleInfo,
    UserEntity,
)

from petty_cash.tests.conftest import headers, make_currency, make_entity, make_user, url

pytestmark = pytest.mark.django_db


def seed_methods(entity, *, electronic=("Visa", "Alipay"), delivery=("Foodpanda",), own=("Alipay",)) -> dict:
    """A link per company ordered 1..n within its type. A name in ``own`` is the company's own row
    (``sale_info.entity_id``, minty-db v0.7); the rest are Minty's catalogue. name -> sale id."""
    ids = {}
    for kind, names in ((SaleType.ELECTRONIC, electronic), (SaleType.DELIVERY, delivery)):
        for order, name in enumerate(names, start=1):
            owner = entity.id if name in own else None
            sale = SaleInfo.objects.filter(sale_name=name, entity_id=owner).first() or SaleInfo.objects.create(
                sale_name=name, type=kind, value_name=name.lower().replace(" ", "_") + "_sales", enabled=True,
                entity_id=owner)
            EntitySaleSetting.objects.create(entity_id=entity.id, sale_id=sale.id, is_active=True, display_order=order)
            ids[name] = str(sale.id)
    return ids


@pytest.fixture
def methods(shop):
    sg = make_currency("SGD")
    CountryInfo.objects.create(country_code="SG", alpha3_code="SGP", country_name_en="Singapore", currency_id=sg.id,
                               is_active=True, display_order=2)
    return seed_methods(shop.entity)


def call(client, shop, method, path, body=None, user=None):
    kwargs = {"content_type": "application/json"} if body is not None else {}
    return getattr(client, method)(url(shop.entity.id, path), body, headers=headers((user or shop.owner).id), **kwargs)


def names(body, kind):
    return [m["name"] for m in body["sales_methods"][kind]]


def member(shop, role, email="cashier@test.com"):
    person = make_user(email, first_name="Cass")
    UserEntity.objects.create(user_id=person.id, entity_id=shop.entity.id, role=role, approved=True)
    return person


# --- the read ----------------------------------------------------------------------------------------


def test_the_settings_read(client, shop, methods):
    resp = call(client, shop, "get", "/settings")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert "country" not in body and body["sales_groups"] == "electronic_delivery"
    assert names(body, "electronic") == ["Visa", "Alipay"] and names(body, "delivery") == ["Foodpanda"]
    # owner, 2026-11-12: the company edits and deletes only its own; Minty's only come off its list
    assert {m["name"]: m["own"] for m in body["sales_methods"]["electronic"]} == {"Visa": False, "Alipay": True}
    assert body["xero_connected"] is False and all(body["permissions"].values())
    assert "edit_xero" in body["permissions"] and "edit_country" not in body["permissions"]


@pytest.mark.parametrize("status, connected", [("connected", True), ("disconnected", False)])
def test_a_kept_organisation_is_not_a_connection(client, shop, methods, status, connected):
    """A disconnect keeps the organisation (Xero optional, 2026-11-10); only the status says connected."""
    from minty_db.models import Entity

    Entity.objects.filter(id=shop.entity.id).update(xero_org_id="org-1", status=status)

    assert call(client, shop, "get", "/settings").json()["xero_connected"] is connected


def test_another_companys_own_method_is_never_offered(client, shop, methods):
    other = make_entity(shop.owner, name="Other Co")
    seed_methods(other, electronic=("Kpay",), delivery=(), own=("Kpay",))
    SaleInfo.objects.create(sale_name="Octopus", type=SaleType.ELECTRONIC, value_name="octopus_sales")

    offered = [m["name"] for m in call(client, shop, "get", "/settings").json()["available"]["electronic"]]

    assert "Octopus" in offered and "Kpay" not in offered
    kpay = SaleInfo.objects.get(sale_name="Kpay")
    resp = call(client, shop, "post", "/sales-methods", {"type": "electronic", "sale_info_id": str(kpay.id)})
    assert resp.status_code == 422


def test_cash_is_never_listed(client, shop, methods):
    cash = SaleInfo.objects.create(sale_name="Cash", type=SaleType.OTHER, value_name="cash_sales")
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=cash.id, is_active=True, display_order=0)

    body = call(client, shop, "get", "/settings").json()

    assert "Cash" not in names(body, "electronic") + names(body, "delivery")
    assert "Cash" not in [m["name"] for group in body["available"].values() for m in group]


def test_a_cashier_reads_settings_but_may_change_nothing(client, shop, methods):
    resp = call(client, shop, "get", "/settings", user=member(shop, EntityRole.CASHIER))

    assert resp.status_code == 200
    permissions = resp.json()["permissions"]
    assert permissions.pop("view_sales_methods") is True and not any(permissions.values())


def test_a_base_member_cannot_open_settings(client, shop, methods):
    resp = call(client, shop, "get", "/settings", user=member(shop, EntityRole.ENTITY_BASE, "base@test.com"))

    assert resp.status_code == 403


# --- country & currency: not here ---------------------------------------------------------------------


def test_the_country_and_currency_are_not_set_here(client, shop, methods):
    """Entity & Integration (minty-web, through Flask) is their one place (owner, 2026-11-03)."""
    resp = call(client, shop, "put", "/settings/country", {"country_code": "SG"})

    assert resp.status_code in (404, 405)
    org = Entity.objects.get(id=shop.entity.id)
    assert (org.country_code, org.currency_id) == ("HK", shop.currency.id)


# --- sales methods -----------------------------------------------------------------------------------


def test_adding_a_method_from_the_catalogue_or_by_name(client, shop, methods):
    octopus = SaleInfo.objects.create(sale_name="Octopus", type=SaleType.ELECTRONIC, value_name="octopus_sales")
    offered = call(client, shop, "get", "/settings").json()["available"]["electronic"]
    assert str(octopus.id) in [m["id"] for m in offered]

    added = call(client, shop, "post", "/sales-methods", {"type": "electronic", "sale_info_id": str(octopus.id)})
    assert added.status_code == 201, added.json()
    assert names(added.json(), "electronic") == ["Visa", "Alipay", "Octopus"]
    assert added.json()["message"] == "Octopus added."

    typed = call(client, shop, "post", "/sales-methods", {"type": "delivery", "name": " Keeta "})
    assert typed.status_code == 201 and names(typed.json(), "delivery") == ["Foodpanda", "Keeta"]
    keeta = SaleInfo.objects.get(sale_name="Keeta")
    # a typed name is the company's own, never a new Minty method
    assert (keeta.type, keeta.value_name, keeta.entity_id) == ("delivery", "keeta_sales", shop.entity.id)


def test_a_typed_name_that_is_mintys_links_mintys_and_another_companys_stays_theirs(client, shop, methods):
    octopus = SaleInfo.objects.create(sale_name="Octopus", type=SaleType.ELECTRONIC, value_name="octopus_sales")
    other = make_entity(shop.owner, name="Other Co")
    theirs = seed_methods(other, electronic=("Kpay",), delivery=(), own=("Kpay",))["Kpay"]

    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "octopus"}).status_code == 201
    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "Kpay"}).status_code == 201

    assert SaleInfo.objects.filter(sale_name__iexact="octopus").count() == 1
    assert EntitySaleSetting.objects.filter(entity_id=shop.entity.id, sale_id=octopus.id, is_active=True).exists()
    mine = SaleInfo.objects.get(sale_name="Kpay", entity_id=shop.entity.id)
    assert str(mine.id) != theirs


def test_a_removed_method_added_again_switches_its_link_back_on(client, shop, methods):
    call(client, shop, "delete", f"/sales-methods/{methods['Visa']}")

    again = call(client, shop, "post", "/sales-methods", {"type": "electronic", "sale_info_id": methods["Visa"]})

    assert again.status_code == 201 and "Visa" in names(again.json(), "electronic")
    assert EntitySaleSetting.objects.filter(entity_id=shop.entity.id, sale_id=methods["Visa"]).count() == 1


@pytest.mark.parametrize("body, status, message", [
    ({"type": "other", "name": "Cheque"}, 422, "Choose Electronic or Delivery for the method."),
    ({"type": "electronic", "name": "  "}, 422, "The method's name is still empty. Type a name to add it."),
    ({"type": "electronic", "sale_info_id": "nope"}, 422, "Pick one of the methods offered, or type a new name."),
    ({"type": "electronic", "name": "visa"}, 409, "visa is already on the list."),
])
def test_a_bad_method_is_refused(client, shop, methods, body, status, message):
    resp = call(client, shop, "post", "/sales-methods", body)

    assert resp.status_code == status and resp.json() == {"error": message}


def test_rule_a_new_name_must_fit_the_catalogue(client, shop, methods):
    """Deliberate difference from Flask (100): a new method's value_name is its name + "_sales" and
    both columns hold 80 characters, so 74 is the most a new name can be."""
    longest = "x" * 74
    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": longest}).status_code == 201
    assert SaleInfo.objects.get(sale_name=longest).value_name == longest + "_sales"

    resp = call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "y" * 75})
    assert resp.status_code == 422
    assert resp.json() == {"error": "That name is too long. Keep it to 74 characters or fewer."}


def test_rule_a_rename_must_fit_the_catalogue(client, shop, methods):
    assert call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": "z" * 80}).status_code == 200

    resp = call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": "z" * 81})
    assert resp.status_code == 422 and resp.json()["error"] == "That name is too long. Keep it to 80 characters or fewer."


def test_renaming_removing_and_ordering(client, shop, methods):
    visa, alipay = methods["Visa"], methods["Alipay"]

    ordered = call(client, shop, "put", "/sales-methods/order", {"type": "electronic", "method_ids": [alipay, visa]})
    assert ordered.status_code == 200, ordered.json()
    assert names(ordered.json(), "electronic") == ["Alipay", "Visa"] and names(ordered.json(), "delivery") == ["Foodpanda"]

    stale = call(client, shop, "put", "/sales-methods/order", {"type": "electronic", "method_ids": [visa]})
    assert stale.status_code == 409

    renamed = call(client, shop, "put", f"/sales-methods/{alipay}", {"name": "AlipayHK"})
    assert renamed.status_code == 200, renamed.json()
    assert names(renamed.json(), "electronic") == ["AlipayHK", "Visa"] and renamed.json()["message"] == "Name saved."

    # Minty's method only comes off the company's list: offered again, the link kept
    removed = call(client, shop, "delete", f"/sales-methods/{visa}")
    assert removed.status_code == 200 and names(removed.json(), "electronic") == ["AlipayHK"]
    assert removed.json()["message"] == "Visa is off your list. You can add it back any time."
    assert visa in [m["id"] for m in removed.json()["available"]["electronic"]]
    assert EntitySaleSetting.objects.get(entity_id=shop.entity.id, sale_id=visa).is_active is False  # the link stays


def test_mintys_method_is_never_renamed_and_a_taken_name_is_refused(client, shop, methods):
    mintys = call(client, shop, "put", f"/sales-methods/{methods['Visa']}", {"name": "VISA Card"})
    assert mintys.status_code == 403
    assert mintys.json() == {
        "error": "Visa comes with Minty, so it can't be renamed. Take it off your list and add your own method instead."
    }

    for taken_name in ("foodpanda", "VISA"):
        taken = call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": taken_name})
        assert taken.status_code == 409 and taken.json() == {"error": "A method with that name already exists."}
    assert SaleInfo.objects.get(id=methods["Alipay"]).sale_name == "Alipay"


def test_the_companys_own_method_is_deleted_completely(client, shop, methods):
    """owner, 2026-11-12: deleting one's own removes it from the list and from every Add choice; a
    report that used it keeps its amount (the row stays, switched off)."""
    alipay = methods["Alipay"]
    removed = call(client, shop, "delete", f"/sales-methods/{alipay}")
    assert removed.status_code == 200 and removed.json()["message"] == "Alipay deleted."
    assert alipay not in [m["id"] for m in removed.json()["available"]["electronic"]]
    assert not SaleInfo.objects.filter(id=alipay).exists()
    assert not EntitySaleSetting.objects.filter(sale_id=alipay).exists()

    kept = seed_methods(shop.entity, electronic=("Kpay",), delivery=(), own=("Kpay",))["Kpay"]
    report = Report.objects.create(entity_id=shop.entity.id, transaction_date="2026-11-01")
    ReportSale.objects.create(report_id=report.id, sale_id=kept, amount=12)
    used = call(client, shop, "delete", f"/sales-methods/{kept}")
    assert used.status_code == 200 and "Kpay" not in names(used.json(), "electronic")
    assert kept not in [m["id"] for m in used.json()["available"]["electronic"]]
    assert SaleInfo.objects.get(id=kept).enabled is False
    assert str(ReportSale.objects.get(report_id=report.id).sale_id) == kept


def test_cash_and_unknown_methods_are_not_found(client, shop, methods):
    cash = SaleInfo.objects.create(sale_name="Cash", type=SaleType.OTHER, value_name="cash_sales")
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=cash.id, is_active=True, display_order=0)

    for method_id in (str(cash.id), "6d0b6d55-0000-4000-8000-000000000000", "nope"):
        assert call(client, shop, "delete", f"/sales-methods/{method_id}").status_code == 404
        assert call(client, shop, "put", f"/sales-methods/{method_id}", {"name": "X"}).status_code == 404


def test_a_cashier_may_not_change_the_methods(client, shop, methods):
    person = member(shop, EntityRole.CASHIER)

    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "Octopus"},
                user=person).status_code == 403
    assert call(client, shop, "delete", f"/sales-methods/{methods['Visa']}", user=person).status_code == 403


# --- the sales accounts (owner, 2026-11-17; Figma 464:73 row 4) ---------------------------------------
# Under the account codes: Sales - Cash, Electronic Payment, Delivery on or off. Cash is the cash answer;
# the other two are entities.sells_electronic / sells_delivery. Turning one off keeps its methods (they
# come back when it is turned on again); the daily report just stops asking for that part.


def _accounts(client, shop, cash, electronic, delivery, user=None):
    return call(client, shop, "put", "/settings/sales-accounts",
                {"cash": cash, "electronic": electronic, "delivery": delivery}, user=user)


def test_the_read_carries_the_sales_accounts(client, shop, methods):
    Entity.objects.filter(id=shop.entity.id).update(business_type="retail", uses_sales_cash=True)
    body = call(client, shop, "get", "/settings").json()
    assert body["sales_parts"] == {"cash": True, "electronic": True, "delivery": False}
    assert body["permissions"]["edit_sales_accounts"] is True


def test_saving_the_sales_accounts_keeps_the_methods(client, shop, methods):
    # every company is linked to Minty's Cash method when it is made (onboarding's entity_create)
    cash = SaleInfo.objects.create(sale_name="Cash", value_name="cash_sales", type=SaleType.OTHER, display_order=0)
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=cash.id, is_active=True, display_order=0)
    resp = _accounts(client, shop, False, True, False)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["sales_parts"] == {"cash": False, "electronic": True, "delivery": False}
    assert body["message"] == "Your sales accounts are saved."
    assert names(body, "delivery") == ["Foodpanda"], "turning Delivery off keeps its methods"
    shop.entity.refresh_from_db()
    assert (shop.entity.uses_sales_cash, shop.entity.sells_electronic, shop.entity.sells_delivery) == (
        False, True, False)

    assert EntitySaleSetting.objects.get(entity_id=shop.entity.id, sale_id=cash.id).is_active is False
    assert _accounts(client, shop, True, True, True).json()["sales_parts"] == {
        "cash": True, "electronic": True, "delivery": True}


def test_none_on_is_refused(client, shop, methods):
    resp = _accounts(client, shop, False, False, False)
    assert resp.status_code == 422
    assert resp.json()["error"] == "No sales account is ticked yet. Tick at least one to continue."
    shop.entity.refresh_from_db()
    assert shop.entity.sells_electronic is None


@pytest.mark.parametrize("body", [{"cash": True, "electronic": "yes", "delivery": False},
                                  {"cash": True, "electronic": True}])
def test_each_account_must_be_on_or_off(client, shop, methods, body):
    assert call(client, shop, "put", "/settings/sales-accounts", body).status_code == 422


def test_a_cashier_cannot_change_the_sales_accounts(client, shop, methods):
    cashier = member(shop, EntityRole.CASHIER)
    assert call(client, shop, "get", "/settings", user=cashier).json()["permissions"]["edit_sales_accounts"] is False
    assert _accounts(client, shop, True, True, False, user=cashier).status_code == 403
    shop.entity.refresh_from_db()
    assert shop.entity.sells_delivery is None


# --- the whole card in one save (2026-11-20) ----------------------------------------------------------
# The hub's "Sales accounts and methods" card holds every tick and method change until Save changes,
# then sends one PUT /settings/sales. It is one transaction: any refusal writes nothing.

ALL_ON = {"cash": True, "electronic": True, "delivery": True}


@pytest.fixture
def cash(shop):
    """Every company is linked to Minty's Cash method when it is made (onboarding's entity_create)."""
    row = SaleInfo.objects.create(sale_name="Cash", value_name="cash_sales", type=SaleType.OTHER, display_order=0)
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=row.id, is_active=True, display_order=0)
    return row


def _save(client, shop, methods_body, accounts=None, user=None):
    return call(client, shop, "put", "/settings/sales",
                {"accounts": accounts or ALL_ON, "methods": methods_body}, user=user)


def _kept(ids, *names_):
    return [{"id": ids[n]} for n in names_]


def _snapshot(shop):
    """What a refused save must leave as it was: the lists (id, name, order), the ticks, the rows."""
    shop.entity.refresh_from_db()
    links = sorted(EntitySaleSetting.objects.filter(entity_id=shop.entity.id)
                   .values_list("sale_id", "is_active", "display_order"), key=str)
    rows = sorted(SaleInfo.objects.values_list("id", "sale_name", "enabled"), key=str)
    return links, rows, (shop.entity.uses_sales_cash, shop.entity.sells_electronic, shop.entity.sells_delivery)


def test_one_save_applies_ticks_adds_renames_removals_and_order(client, shop, methods, cash):
    octopus = SaleInfo.objects.create(sale_name="Octopus", type=SaleType.ELECTRONIC, value_name="octopus_sales")

    resp = _save(client, shop, {
        # Visa (Minty's) left out = off the list; Alipay renamed and moved first; Octopus picked; Keeta typed
        "electronic": [{"id": methods["Alipay"], "name": "AlipayHK"}, {"sale_info_id": str(octopus.id)}],
        "delivery": [{"name": " Keeta "}, {"id": methods["Foodpanda"], "name": "Foodpanda"}],
    }, accounts={"cash": False, "electronic": True, "delivery": True})

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["message"] == "Your sales accounts and methods are saved."
    assert names(body, "electronic") == ["AlipayHK", "Octopus"]
    assert names(body, "delivery") == ["Keeta", "Foodpanda"]
    assert body["sales_parts"] == {"cash": False, "electronic": True, "delivery": True}
    assert methods["Visa"] in [m["id"] for m in body["available"]["electronic"]]
    assert EntitySaleSetting.objects.get(entity_id=shop.entity.id, sale_id=methods["Visa"]).is_active is False
    keeta = SaleInfo.objects.get(sale_name="Keeta")
    assert (keeta.type, keeta.value_name, keeta.entity_id) == ("delivery", "keeta_sales", shop.entity.id)


def test_one_save_deletes_an_unused_own_method_and_switches_off_a_used_one(client, shop, methods, cash):
    kpay = seed_methods(shop.entity, electronic=("Kpay",), delivery=(), own=("Kpay",))["Kpay"]
    report = Report.objects.create(entity_id=shop.entity.id, transaction_date="2026-11-01")
    ReportSale.objects.create(report_id=report.id, sale_id=kpay, amount=12)

    resp = _save(client, shop, {"electronic": _kept(methods, "Visa"), "delivery": _kept(methods, "Foodpanda")})

    assert resp.status_code == 200, resp.json()
    assert names(resp.json(), "electronic") == ["Visa"]
    assert not SaleInfo.objects.filter(id=methods["Alipay"]).exists()
    assert SaleInfo.objects.get(id=kpay).enabled is False, "a report used it: switched off, not deleted"
    assert str(ReportSale.objects.get(report_id=report.id).sale_id) == kpay


def test_a_type_left_out_of_the_save_stays_as_it_is(client, shop, methods, cash):
    resp = _save(client, shop, {"electronic": _kept(methods, "Visa", "Alipay")})

    assert resp.status_code == 200, resp.json()
    assert names(resp.json(), "delivery") == ["Foodpanda"]


def test_saving_nothing_changed_is_fine(client, shop, methods, cash):
    before = _snapshot(shop)
    resp = _save(client, shop, {"electronic": _kept(methods, "Visa", "Alipay"), "delivery": _kept(methods, "Foodpanda")},
                 accounts={"cash": True, "electronic": True, "delivery": True})
    assert resp.status_code == 200, resp.json()
    links, rows, _ticks = _snapshot(shop)
    assert (links, rows) == before[:2]


@pytest.mark.parametrize("methods_body, accounts, status, message", [
    # a rename of Minty's method, after the removal of Visa that the save would have written first
    (lambda ids: {"electronic": [{"id": ids["Alipay"]}],
                  "delivery": [{"id": ids["Foodpanda"], "name": "Panda"}]}, None, 403,
     "Foodpanda comes with Minty, so it can't be renamed. Take it off your list and add your own method instead."),
    (lambda ids: {"electronic": [{"id": ids["Visa"]}, {"id": ids["Alipay"], "name": "foodpanda"}]}, None, 409,
     "A method with that name already exists."),
    (lambda ids: {"electronic": [{"id": "6d0b6d55-0000-4000-8000-000000000000"}]}, None, 404,
     "That sales method isn't on this company's list."),
    (lambda ids: {"electronic": [{"id": ids["Foodpanda"]}]}, None, 404,
     "That sales method isn't on this company's list."),
    (lambda ids: {"electronic": [{"id": ids["Visa"]}, {"name": "visa"}]}, None, 409, "visa is already on the list."),
    (lambda ids: {"electronic": [{"id": ids["Alipay"]}, {"name": "Kpay"}, {"name": "KPAY"}]}, None, 409,
     "KPAY is already on the list."),
    (lambda ids: {"electronic": [{"name": "  "}]}, None, 422, "The method's name is still empty. Type a name to add it."),
    (lambda ids: {"electronic": [{"name": "y" * 75}]}, None, 422,
     "That name is too long. Keep it to 74 characters or fewer."),
    (lambda ids: {"electronic": [{"id": ids["Alipay"], "name": " "}]}, None, 422,
     "The method's name is still empty. Type a name to save it."),
    (lambda ids: {"electronic": [{"sale_info_id": "nope"}]}, None, 422,
     "Pick one of the methods offered, or type a new name."),
    (lambda ids: {"electronic": []}, {"cash": False, "electronic": False, "delivery": False}, 422,
     "No sales account is ticked yet. Tick at least one to continue."),
    (lambda ids: {"electronic": []}, {"cash": True, "electronic": "yes", "delivery": False}, 422,
     "Turn each sales account on or off to continue."),
    (lambda ids: {"other": []}, None, 422, "Send each type's methods in their order to save them."),
    (lambda ids: {"electronic": "Visa"}, None, 422, "Send each type's methods in their order to save them."),
    (lambda ids: {"electronic": [{"id": ids["Visa"]}, {"id": ids["Visa"]}]}, None, 422,
     "Send each type's methods in their order to save them."),
])
def test_a_refused_save_writes_nothing(client, shop, methods, cash, methods_body, accounts, status, message):
    before = _snapshot(shop)

    resp = _save(client, shop, methods_body(methods), accounts=accounts)

    assert resp.status_code == status and resp.json() == {"error": message}
    assert _snapshot(shop) == before


def test_a_cashier_cannot_save_the_card(client, shop, methods, cash):
    before = _snapshot(shop)
    resp = _save(client, shop, {"electronic": []}, user=member(shop, EntityRole.CASHIER))
    assert resp.status_code == 403
    assert _snapshot(shop) == before
