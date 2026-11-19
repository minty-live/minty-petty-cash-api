"""The report wizard's Sales step - Flask's ``blueprints/report/services/sales.py`` (and the totals
helpers it calls in ``shared.py``), ported.

Flask's rules: a save needs the day's draft; every method the page shows gets a ``report_sale``
row, a method left out a zero; methods the page has no field for (an ``other`` one that is not
Cash) are left alone; the closing balance is recomputed AFTER the rows are written; the saver
becomes the report's creator ("track last editor").
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from minty_db.enums import SaleType
from minty_db.models import Entity, EntitySaleSetting, Report, ReportSale, SaleInfo

from petty_cash.money import cents
from petty_cash.services.reports import (
    NOT_SENT,
    check_version,
    log_history,
    mark_saved,
    update_draft_progress,
)

#: Cash is the sales-method catalogue's ``other`` row whose ``value_name`` is this.
CASH_VALUE_NAME = "cash_sales"

#: A method the page lists but the save has always skipped (the old Deliveroo column).
SKIPPED = "deliveroo_sales"

#: Which lists the pages show (owner, 2026-11-11; onboarding's step 5 asks the same):
#: Restaurant & Food takes Electronic and Delivery; a retail shop, a service or a general business
#: takes one Non-cash list (its methods are stored as electronic). A company from before onboarding
#: asked the business type keeps both.
NON_CASH_BUSINESSES = ("retail", "service", "general")


def sales_groups(entity_id) -> str:
    """``non_cash`` or ``electronic_delivery`` - how the pages group the company's methods."""
    business = Entity.objects.filter(id=entity_id).values_list("business_type", flat=True).first()
    return "non_cash" if business in NON_CASH_BUSINESSES else "electronic_delivery"


def sales_parts(entity_id) -> dict:
    """Which of the page's three parts the company takes - onboarding's sales accounts (owner,
    2026-11-17; Figma 464:73 row 3). Cash Sales is the cash answer (NULL counts as yes); Electronic
    Payments and Delivery Sales are the company's ticks (minty-db v0.11), NULL until chosen, when the
    business type picks: Restaurant & Food and no type both, a retail shop, a service or a general
    business Electronic (its Non-cash list) only. minty-onboarding-api's ``sales_ticks`` says the same."""
    row = (
        Entity.objects.filter(id=entity_id)
        .values("business_type", "uses_sales_cash", "sells_electronic", "sells_delivery")
        .first()
        or {}
    )
    electronic, delivery = row.get("sells_electronic"), row.get("sells_delivery")
    # "None of these" (no type, the question answered): Cash Sales only until chosen (owner,
    # 2026-11-18). A company from before the questions (no answer either) keeps every part.
    none_of_these = row.get("business_type") is None and row.get("uses_sales_cash") is not None
    return {
        "cash": row.get("uses_sales_cash") is not False,
        "electronic": (not none_of_these) if electronic is None else electronic,
        "delivery": (row.get("business_type") not in NON_CASH_BUSINESSES and not none_of_these)
        if delivery is None else delivery,
    }


@transaction.atomic
def set_sales_parts(entity_id, cash: bool, electronic: bool, delivery: bool) -> None:
    """Settings' sales accounts (owner, 2026-11-17). Cash is the cash answer, so the company's Cash
    method follows it, as onboarding's 4.2 does (linked when it has none); the methods of a part that
    is off stay as they are, so turning it on again brings them back."""
    Entity.objects.filter(id=entity_id).update(
        uses_sales_cash=cash, sells_electronic=electronic, sells_delivery=delivery
    )
    changed = EntitySaleSetting.objects.filter(entity_id=entity_id, sale__value_name=CASH_VALUE_NAME).update(
        is_active=cash
    )
    if cash and not changed:
        row = SaleInfo.objects.filter(value_name=CASH_VALUE_NAME, entity_id__isnull=True).first()
        if row is None:
            raise RuntimeError("Minty's Cash sales method is missing from sale_info")
        EntitySaleSetting.objects.create(entity_id=entity_id, sale_id=row.id, is_active=True, display_order=0)


def sales_methods(entity_id) -> list[dict]:
    """The company's enabled methods in Settings order (``display_order``, then name), each with
    its catalogue row's ``sale_name`` / ``value_name`` / ``type``."""
    links = list(
        EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).values("sale_id", "display_order")
    )
    catalogue = {
        row["id"]: row
        for row in SaleInfo.objects.filter(id__in=[link["sale_id"] for link in links]).values(
            "id", "sale_name", "value_name", "type"
        )
    }
    methods = [
        {**catalogue[link["sale_id"]], "sale_id": link["sale_id"], "display_order": link["display_order"]}
        for link in links
        if link["sale_id"] in catalogue
    ]
    # Postgres sorts a NULL display_order last in ascending order (Flask's query relies on it).
    methods.sort(key=lambda m: (m["display_order"] is None, m["display_order"] or 0, m["sale_name"]))
    return methods


def shown(method: dict, parts: dict | None = None) -> bool:
    """The page has a field for the electronic and delivery methods, and for Cash - each only while
    its part is on (``sales_parts``). A Non-cash list with no Delivery part keeps any delivery method
    in it, as before."""
    if parts is None:
        parts = {"cash": True, "electronic": True, "delivery": True}
    if method["value_name"] == CASH_VALUE_NAME:
        return parts["cash"]
    if method["type"] == SaleType.ELECTRONIC:
        return parts["electronic"]
    if method["type"] == SaleType.DELIVERY:
        return parts["delivery"] or (parts["electronic"] and method.get("non_cash", False))
    return False


def page_methods(entity_id, report_id=None) -> list[dict]:
    """The methods the page shows, in order, each with the report's amount (None without a row)."""
    amounts = {}
    if report_id is not None:
        amounts = dict(ReportSale.objects.filter(report_id=report_id).values_list("sale_id", "amount"))
    parts, non_cash = sales_parts(entity_id), sales_groups(entity_id) == "non_cash"
    return [
        {
            "sale_id": str(m["sale_id"]),
            "name": m["sale_name"],
            "value_name": m["value_name"],
            "type": str(m["type"]),
            "amount": amounts.get(m["sale_id"]),
        }
        for m in sales_methods(entity_id)
        if m["value_name"] and shown({**m, "non_cash": non_cash}, parts)
    ]


def cash_sales_from_detail(report: Report) -> Decimal:
    """The Cash method's row, else the report's cash column (Flask's ``get_cash_sales_from_detail``)."""
    cash_id = SaleInfo.objects.filter(value_name=CASH_VALUE_NAME).values_list("id", flat=True).first()
    total = sum(
        (cents(a) for a in ReportSale.objects.filter(report_id=report.id, sale_id=cash_id).values_list("amount", flat=True)),
        Decimal("0.00"),
    ) if cash_id else Decimal("0.00")
    return total if total > 0 else cents(report.cashsale_total)


def refresh_sales_totals(report: Report) -> None:
    """``total_sales`` = every method's rows (the column's cash too when no Cash row holds it),
    ``nocashsale_total`` = that less the cash (Flask's ``update_report_draft_sales_from_detail``)."""
    by_sale = {}
    for sale_id, amount in ReportSale.objects.filter(report_id=report.id).values_list("sale_id", "amount"):
        by_sale[sale_id] = by_sale.get(sale_id, Decimal("0.00")) + cents(amount)
    names = dict(SaleInfo.objects.filter(id__in=list(by_sale)).values_list("id", "value_name"))
    # Flask's ``if value_name and amount``: a catalogue row with no value_name, or a zero, adds nothing.
    counted = {sid: v for sid, v in by_sale.items() if names.get(sid) and v}
    cash = cents(report.cashsale_total)
    total = sum(counted.values(), Decimal("0.00"))
    cash_in_rows = any(names[sid] == CASH_VALUE_NAME for sid in counted)
    if cash > 0 and not cash_in_rows:
        total += cash
    report.total_sales = total
    report.nocashsale_total = total - cash


@transaction.atomic
def save_sales(entity_id, user, draft: Report, amounts: dict, action_type: str, opened=NOT_SENT) -> Report:
    """Write the Sales step onto ``draft``: one ``report_sale`` row per method the page shows
    (``amounts`` maps value_name -> amount; a missing method is zero), the totals, the closing
    balance, the progress and a history line - one transaction. A save opened from another
    ``edit_version`` (``opened``) raises ``StepAlreadySaved`` before anything is written."""
    draft = Report.objects.select_for_update().get(id=draft.id)
    check_version(draft, opened)
    draft.created_by = user.id
    cash = cents(amounts.get(CASH_VALUE_NAME) or 0)
    draft.cashsale_total = cash

    shop_total, delivery_total = cash, Decimal("0.00")
    parts, non_cash = sales_parts(entity_id), sales_groups(entity_id) == "non_cash"
    for method in sales_methods(entity_id):
        field = method["value_name"]
        if not field or field == SKIPPED or not shown({**method, "non_cash": non_cash}, parts):
            continue
        amount = cents(amounts.get(field) or 0)
        if field != CASH_VALUE_NAME:
            if method["type"] == SaleType.DELIVERY:
                delivery_total += amount
            else:
                shop_total += amount
        updated = ReportSale.objects.filter(report_id=draft.id, sale_id=method["sale_id"]).update(amount=amount)
        if not updated:
            ReportSale.objects.create(report_id=draft.id, sale_id=method["sale_id"], amount=amount)

    refresh_sales_totals(draft)
    draft.closing_balance = (
        cents(draft.opening_balance)
        + cents(draft.cash_addition)
        + cash_sales_from_detail(draft)
        - cents(draft.expense_total)
        - cents(draft.bank_deposit)
    )
    update_draft_progress(draft, action_type, "sales", "expenses")
    mark_saved(draft, user)
    draft.save()
    log_history(
        draft,
        user,
        action="updated",
        field_changed="sales_entry",
        old_value="previous_sales_state",
        new_value=(
            f"Sales: Shop={float(shop_total)}, Delivery={float(delivery_total)}, "
            f"Total={float(shop_total + delivery_total)}, Closing Balance={float(draft.closing_balance)}"
        ),
    )
    return draft


def report_methods(entity_id, report_id) -> list[dict]:
    """``page_methods`` for a report already filled in: the methods the company has on, then any
    method switched off or removed since that still holds an amount on this report (a submitted
    report keeps what it said). Deliveroo and Cash stay out (Flask's ``report_methods``)."""
    methods = page_methods(entity_id, report_id)
    listed = {m["sale_id"] for m in methods}
    rows = ReportSale.objects.filter(report_id=report_id).values_list("sale_id", "amount")
    catalogue = {s.id: s for s in SaleInfo.objects.filter(id__in=[sale_id for sale_id, _ in rows])}
    for sale_id, amount in rows:
        sale = catalogue.get(sale_id)
        if sale is None or str(sale_id) in listed or not amount or sale.value_name in (SKIPPED, CASH_VALUE_NAME):
            continue
        methods.append({"sale_id": str(sale_id), "name": sale.sale_name, "value_name": sale.value_name,
                        "type": str(sale.type), "amount": amount})
    return methods
