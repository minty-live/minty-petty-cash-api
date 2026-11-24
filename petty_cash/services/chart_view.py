"""Accounts and suppliers as the company's chart shows them NOW (the user's rule, 2026-11-24).

An expense line keeps its account and supplier by id (``report_expense.account_id``/``contact_id``);
every screen, the share link, the CSV and what a new publish sends read the code, name and type here,
so a matching, a merge or a rename in Xero shows everywhere with nothing copied. An account or contact
merged away (archived, ``merged_into_id`` set) shows the one it was merged into. ``report_expense.item``
(the account's name when the line was saved) is shown only for a line with no account; the
``published_*`` columns are the record of what went to Xero and are never shown.
"""

from __future__ import annotations

from dataclasses import dataclass

from minty_db.models import AccountInfo, XeroContactSync

#: How far a merge chain is followed (merges refuse a target that was itself merged away, so it is one).
_MAX_HOPS = 5


@dataclass(frozen=True)
class AccountView:
    id: object
    code: str
    name: str
    type: str
    row: AccountInfo  # the account itself (``expenses.public_id`` names it to the screens)


@dataclass(frozen=True)
class ContactView:
    id: object
    name: str
    row: XeroContactSync


def _follow(ids, model, fields):
    """``{asked id: row}``, each row the end of its ``merged_into`` chain."""
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = {r.id: r for r in model.objects.filter(id__in=wanted).only("id", "merged_into_id", *fields)}
    for _ in range(_MAX_HOPS):
        missing = {r.merged_into_id for r in rows.values() if r.merged_into_id} - rows.keys()
        if not missing:
            break
        rows.update({r.id: r for r in model.objects.filter(id__in=missing).only("id", "merged_into_id", *fields)})
    out = {}
    for i in wanted:
        row = rows.get(i)
        for _ in range(_MAX_HOPS):
            if row is None or not row.merged_into_id or row.merged_into_id not in rows:
                break
            row = rows[row.merged_into_id]
        if row is not None:
            out[i] = row
    return out


def accounts(ids) -> dict:
    """``{account id: AccountView}`` for the ids that exist; the code is Xero's, else Minty's own."""
    return {
        i: AccountView(id=r.id, code=(r.xero_code or r.code or "").strip(), name=(r.name or "").strip(),
                       type=r.type or "", row=r)
        for i, r in _follow(ids, AccountInfo, ("xero_code", "code", "name", "type", "xero_account_id")).items()
    }


def contacts(ids) -> dict:
    """``{contact id: ContactView}`` for the ids that exist."""
    return {i: ContactView(id=r.id, name=(r.name or "").strip(), row=r)
            for i, r in _follow(ids, XeroContactSync, ("name", "xero_contact_id")).items()}


def item_name(line, views: dict) -> str:
    """An expense line's account name now; its saved ``item`` only when it has no account."""
    view = views.get(line.account_id) if line.account_id else None
    return view.name if view is not None else (line.item or "").strip()


def contact_name(line, views: dict) -> str:
    """An expense line's supplier name now ('' for none)."""
    view = views.get(line.contact_id) if line.contact_id else None
    return view.name if view is not None else ""
