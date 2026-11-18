"""Report history's Petty Cash Report CSV (owner, 2026-11-13): the company's daily reports in a
range as a person reads them - each day's report, then its sales and the expenses it recorded with
their account and supplier. The movements CSV (``report_csv.py``) is the bank-statement file beside it. Database
only.

The range, the reports (submitted or published, never a draft), who may export and the file's name
follow ``report_csv.py``; with no range the file is the company's whole history
(``report_csv.every_submitted_report``). One table, by day:

* a ``Daily report`` row: the day's figures (opening balance, cash added, cash and non-cash sales,
  expenses, bank deposit, cash short/over - negative when short - and closing balance; owner,
  2026-11-18: no status or submitter columns);
* a ``Sale`` row per sales method with an amount (owner, 2026-11-18: sales top to bottom), in the
  company's method order, then any method since switched off or removed that still holds an amount
  (by name); a zero is left out. Cash is its method's row, else - as older reports keep it - the
  report's cash column (``sales.cash_sales_from_detail``'s rule);
* an ``Expense`` row per line as entered (``created_at``, then ``id``): its account (code, name),
  supplier, description and amount. A line whose account or supplier row is gone shows what was
  last published to Xero; a line with no words of its own (empty, ``No description`` or ``-``)
  shows its item - publish's rule.

A row's Amount is money into the petty cash positive (a sale) and out negative (an expense; owner,
2026-11-18); the report row's figures keep their own signs. Amounts are plain numbers with two
decimal places; the company's currency code has its own column
(``No currency selected`` when it has none - the money rule, never a silent blank).
"""

from __future__ import annotations

from decimal import Decimal

from minty_db.enums import DiscrepancyType
from minty_db.models import AccountInfo, ReportExpense, ReportSale, SaleInfo, XeroContactSync

from petty_cash.money import cents
from petty_cash.services.report_csv import to_csv as _to_csv
from petty_cash.services.sales import CASH_VALUE_NAME, sales_methods

COLUMNS = (
    "Date", "Line", "Account Code", "Account Name", "Contact", "Description", "Amount",
    "Opening Balance", "Cash Added", "Cash Sales", "Non-cash Sales", "Expenses", "Bank Deposit",
    "Cash Short/Over", "Closing Balance", "Currency",
)
#: The file's name: ``<company>_petty-cash-report_<start>_to_<end>.csv``.
FILE_LABEL = "petty-cash-report"
REPORT_LINE = "Daily report"
SALE_LINE = "Sale"
#: Cash's name when the catalogue has no Cash method to name it.
CASH_NAME = "Cash Sales"
EXPENSE_LINE = "Expense"
NO_CURRENCY = "No currency selected"
#: Publish's rule for an expense line's words: these mean the person typed nothing.
_EMPTY_DESCRIPTIONS = ("", "No description", "-")
_CENT = Decimal("0.01")


def _money(value) -> str:
    """Two decimal places; empty for no value; a zero is ``0.00``, never ``-0.00``."""
    if value is None:
        return ""
    return str(Decimal(value).quantize(_CENT) + 0)


def _difference(report) -> Decimal:
    """The counted difference: negative when short, positive when over, 0 when none."""
    kind, amount = str(report.discrepancy_type or "").strip().lower(), report.discrepancy_amount
    if not kind or kind == "none" or not amount:
        return Decimal(0)
    return -abs(amount) if kind in (DiscrepancyType.SHORT, "shortage") else abs(amount)


def _lines(report_ids) -> dict[str, list]:
    """Each report's expense lines as entered, each with ``account_pair`` (code, name) and
    ``contact_name`` - the stored rows, else what was last published."""
    out: dict[str, list] = {}
    if not report_ids:
        return out
    rows = list(ReportExpense.objects.filter(report_id__in=report_ids).order_by("created_at", "id"))
    account_ids = {r.account_id for r in rows if r.account_id}
    contact_ids = {r.contact_id for r in rows if r.contact_id}
    accounts = {a.id: ((a.xero_code or a.code or "").strip(), (a.name or "").strip())
                for a in AccountInfo.objects.filter(id__in=account_ids)} if account_ids else {}
    contacts = {c.id: (c.name or "").strip()
                for c in XeroContactSync.objects.filter(id__in=contact_ids)} if contact_ids else {}
    for line in rows:
        line.account_pair = accounts.get(line.account_id) or (
            (line.published_account_code or "").strip(), (line.published_account_name or "").strip())
        line.contact_name = contacts.get(line.contact_id) or (line.published_contact_name or "").strip()
        out.setdefault(str(line.report_id), []).append(line)
    return out


def _sales(entity_id, reports) -> dict[str, list[tuple[str, Decimal]]]:
    """Each report's sales as ``(method name, amount)``: one per method with an amount, in the
    company's order (``sales.sales_methods``), then methods no longer on by name. Four queries for
    any number of reports."""
    report_ids = [r.id for r in reports]
    if not report_ids:
        return {}
    stored = list(ReportSale.objects.filter(report_id__in=report_ids).values_list("report_id", "sale_id", "amount"))
    order = {m["sale_id"]: i for i, m in enumerate(sales_methods(entity_id))}
    catalogue = {s.id: s for s in SaleInfo.objects.filter(id__in={sale_id for _, sale_id, _ in stored})}
    cash = next((s for s in catalogue.values() if s.value_name == CASH_VALUE_NAME), None) or         SaleInfo.objects.filter(value_name=CASH_VALUE_NAME).first()

    totals: dict[str, dict] = {}
    for report_id, sale_id, amount in stored:
        sale = catalogue.get(sale_id)
        if sale is None or not sale.value_name:  # refresh_sales_totals counts only named methods
            continue
        by_sale = totals.setdefault(str(report_id), {})
        by_sale[sale_id] = by_sale.get(sale_id, Decimal("0.00")) + cents(amount)

    def place(sale_id):
        return (0, order[sale_id], "") if sale_id in order else (1, 0, catalogue[sale_id].sale_name or "")

    out: dict[str, list[tuple[str, Decimal]]] = {}
    for report in reports:
        by_sale = {sid: v for sid, v in totals.get(str(report.id), {}).items() if v}
        listed = sorted(by_sale, key=place)
        named = [(catalogue[sid].sale_name or "", by_sale[sid]) for sid in listed]
        column = cents(report.cashsale_total)
        if column > 0 and not (cash and cash.id in by_sale):  # cash only in the report's column
            spot = order.get(cash.id) if cash else None
            entry = ((cash.sale_name if cash else None) or CASH_NAME, column)
            if spot is None:
                named.insert(0, entry)
            else:
                named.insert(sum(1 for sid in listed if sid in order and order[sid] < spot), entry)
        out[str(report.id)] = named
    return out


def rows(entity_id, reports, currency: str | None) -> list[list[str]]:
    """The CSV's rows (without the header) for ``reports``, all of ``entity_id``, in their order."""
    lines = _lines([r.id for r in reports])
    sales = _sales(entity_id, reports)
    code = currency or NO_CURRENCY
    out: list[list[str]] = []
    for report in reports:
        day = report.transaction_date.strftime("%Y-%m-%d")
        out.append([
            day, REPORT_LINE, "", "", "", "", "",
            _money(report.opening_balance), _money(report.cash_addition or 0),
            _money(report.cashsale_total or 0), _money(report.nocashsale_total or 0),
            _money(report.expense_total or 0), _money(report.bank_deposit or 0),
            _money(_difference(report)), _money(report.closing_balance), code,
        ])
        for name, amount in sales.get(str(report.id), []):
            out.append([day, SALE_LINE, "", "", "", name, _money(amount), "", "", "", "", "", "", "", "", code])
        for line in lines.get(str(report.id), []):
            words = (line.description or "").strip()
            if words in _EMPTY_DESCRIPTIONS:
                words = (line.item or "").strip() or EXPENSE_LINE
            code_, name = line.account_pair
            out.append([day, EXPENSE_LINE, code_, name, line.contact_name, words,
                        _money(-(line.amount or 0)), "", "", "", "", "", "", "", "", code])
    return out


def to_csv(body) -> bytes:
    """The file: this report's header, then ``body``; UTF-8 without a byte-order mark, LF."""
    return _to_csv(body, columns=COLUMNS)
