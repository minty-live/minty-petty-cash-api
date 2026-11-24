"""Reading a receipt with the model - the Petty Cash half (owner, 2026-11-04).

The prompt is the owner's simple one (2026-11-20; ``minty_shared.ai_prompt``, shared with Payment
Request's invoice reader): seven fields, the company's lists with short ids, JSON only, no
confidence - every value that passes the check is filled and marked for the person to check. This
module brings the company's lists and the check of the reply against them. The engine - the call,
its retries, the switch, the models and their failover, cost and the one log line - is
``minty_shared.ai``; how many reads a company has left, and which models to try, is
``minty_shared.django.ai_allowance`` (minty-db's ``ai_policy``/``ai_read``, one allowance for both
modules; owner, 2026-11-18); the small copy the model reads is ``minty_shared.model_input``.

Brain 1, the model, reads the receipt and applies its own knowledge of what the vendor sells;
Brain 2, Minty, supplies the company's chart of accounts and suppliers; Brain 3, the person, is
the final authority - nothing here writes any record. Every failure is "no suggestion" with a
reason, and the line is typed by hand as before.

Configuration: the ``ai_policy`` row (owner, 2026-11-20) - ``receipt_reader_on`` is the switch, and
the models, the Vertex AI region, the timeout, the token budget, the thinking level and the
largest receipt are there too (``ai_allowance.Policy.reader``). The environment names only Vertex
AI's project, ``GOOGLE_CLOUD_PROJECT``, with ``GOOGLE_APPLICATION_CREDENTIALS``.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from minty_db.models import CurrencyInfo, Entity
from minty_shared import ai, ai_prompt, fx, names
from minty_shared.django import ai_allowance
from minty_shared.model_input import prepare_for_model

from petty_cash.days import today
from petty_cash.services.expenses import expense_accounts, public_id, suppliers

logger = logging.getLogger("petty-cash-api")

_MAX_AMOUNT = Decimal("10000000")
# A receipt in another currency is capped only after conversion (``fx.exchange_for``); as read it
# need only be a sane number (20,000,000 KRW is about HKD 112,000).
_MAX_FOREIGN_AMOUNT = Decimal("999999999999")
_MAX_DESCRIPTION = 200


#: The reply's model, the owner's simple prompt (``minty_shared.ai_prompt``, 2026-11-20).
Reading = ai_prompt.SimpleReading


def build_context(entity_id) -> dict | None:
    """The company's facts and the lists a line can use - the same ``suppliers`` and
    ``expense_accounts`` the Expenses step offers, so nothing is suggested that cannot then be
    chosen. Sorted so the cached prefix stays byte-stable: accounts by (code, id), suppliers by
    (name, id). None when there is nothing to choose from. Nothing else is sent: no history, no
    totals, no person's data."""
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        return None
    accounts = sorted(
        ({"id": public_id(a), "code": a.xero_code or "", "name": a.name or ""}
         for a in expense_accounts(entity_id)),
        key=lambda a: (a["code"], a["id"]),
    )
    contacts = sorted(
        ({"id": public_id(c), "name": c.name or ""}
         for c in suppliers(entity_id)),
        key=lambda c: (c["name"], c["id"]),
    )
    if not accounts and not contacts:
        return None
    currency = ""
    if entity.currency_id:
        row = CurrencyInfo.objects.filter(id=entity.currency_id).first()
        currency = (row.currency_code or "").strip() if row else ""
    return {
        "entity": {"id": str(entity.id), "name": entity.name or "",
                   "country": entity.country_code or "", "currency": currency},
        "accounts": accounts,
        "contacts": contacts,
    }


def prompt(context: dict) -> tuple[str, str, ai_prompt.Aliases]:
    """``(instruction, prefix, aliases)``: the owner's simple prompt over this company's lists, with
    short ids (``acc_01``, ``con_01``) that ``aliases`` maps back to the lists' ids."""
    entity = context["entity"]
    return ai_prompt.build(
        ai_prompt.RECEIPT,
        {"name": entity["name"], "country": entity["country"], "currency": entity["currency"]},
        [(a["id"], a["code"], a["name"]) for a in context["accounts"]],
        [(c["id"], c["name"]) for c in context["contacts"]],
    )


def validate(parsed: Reading, context: dict, aliases: ai_prompt.Aliases) -> dict:
    """The reply is untrusted input: a short id is accepted only if it was in the lists sent with
    THIS request, and a value that fails a check is dropped, never repaired. There is no confidence
    (owner, 2026-11-20): every value that passes is filled and marked for the person to check."""
    accounts = {a["id"]: a for a in context["accounts"]}
    contacts = {c["id"]: c for c in context["contacts"]}

    # The name as printed (untrusted text: spaces collapsed, capped) - shown beside the chosen
    # supplier so a wrong "similar" pick is easy to spot (the user, 2026-11-23).
    name = " ".join((parsed.supplier_name or "").split())[:ai.NEW_SUPPLIER_MAX_NAME]
    read = {"read_name": name} if name else {}
    supplier_id = aliases.supplier(parsed.supplier_contact_id) or ""
    if supplier_id not in contacts:
        if (parsed.supplier_contact_id or "").strip():
            logger.info("receipt reader: dropped a supplier id not in the sent list")
        # the list's supplier of the same name (case, spaces, punctuation ignored) is that supplier,
        # never offered as a new one (the user, 2026-11-23)
        supplier_id = next((c["id"] for c in context["contacts"] if names.same_name(c["name"], name)), "")
    if supplier_id:
        supplier = ai.checked(contacts[supplier_id]["name"], {"contact_id": supplier_id, **read})
    else:
        # No match: the name read off the receipt is handed on as `detected_name`, with
        # `new_supplier_token` - the name signed for this company - which saving the line turns into
        # the supplier (the user, 2026-11-23: a missing contact is added automatically, at save,
        # never here).
        extra = {"contact_id": "", "detected_name": name, **read}
        if name:
            extra["new_supplier_token"] = ai.new_supplier_token(settings.SECRET_KEY,
                                                                context["entity"]["id"], name)
        supplier = ai.checked("", extra)

    account_id = aliases.account(parsed.account_id) or ""
    if account_id in accounts:
        account = accounts[account_id]
        account_field = ai.checked(f"{account['code']} {account['name']}".strip(),
                                   {"account_id": account_id, "code": account["code"],
                                    "name": account["name"]})
    else:
        if (parsed.account_id or "").strip():
            logger.info("receipt reader: dropped an account id not in the sent list")
        account_field = ai.checked("", {"account_id": "", "code": "", "name": ""})

    currency = (parsed.currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = ""
    foreign = bool(currency) and currency != context["entity"]["currency"]

    amount_value = ""
    try:
        amount = Decimal(str(parsed.amount or 0)).quantize(Decimal("0.01"))
        if Decimal("0") < amount <= (_MAX_FOREIGN_AMOUNT if foreign else _MAX_AMOUNT):
            amount_value = format(amount, "f")
    except (InvalidOperation, ValueError, TypeError):
        amount_value = ""

    return {
        "supplier": supplier,
        "account": account_field,
        "amount": ai.checked(amount_value),
        "description": _description(parsed),
        "document_date": ai.checked(_iso_date(parsed.date)),
        "currency": currency,
        "entity_currency": context["entity"]["currency"],
        "document_count": parsed.document_count,
        "document_type": parsed.document_type,
    }


def _description(parsed: Reading) -> dict:
    """What was bought (owner, 2026-11-23): inferred from the supplier and context when the
    receipt lists no items - then in the ``low`` band, so the page asks for a closer check."""
    text = (parsed.description or "").strip()[:_MAX_DESCRIPTION]
    inferred = bool(text) and parsed.description_inferred
    return ai.checked(text, {"inferred": inferred, **({"band": "low"} if inferred else {})})


def _iso_date(value: object) -> str:
    """A date the model wrote as YYYY-MM-DD, or "" - never repaired."""
    text = str(value or "").strip()[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def reader_status(entity_id) -> dict:
    """The page's ``reader``: whether "Read with AI" is offered, and the company's reads left."""
    return ai_allowance.reader_status(ai_allowance.READER_RECEIPT, entity_id)


def read_receipt(entity_id, user_id, data: bytes, mime: str, report_date: date | None = None) -> dict:
    """``{suggestions, reason, reader}`` for a receipt the route has checked (who asks, one
    PDF/JPEG/PNG/HEIC by its bytes, at most 10 MB; a HEIC reaches the model as JPEG through
    ``prepare_for_model``). ``suggestions`` is null on every failure; every
    outcome is the caller's 200. ``reader`` is the allowance after this read, so the page's count
    moves without another call. A read is reserved before the model is called and counts only if
    a model answered (``ai_allowance``); a refusal before that point uses nothing. A receipt in
    another currency comes back converted (``fx.exchange_for``; ``report_date`` is the rate's day
    when the receipt has none, the company's today when not given)."""
    policy = ai_allowance.load_policy()
    if policy is None:
        return {"suggestions": None, "reason": ai.REASON_NOT_CONFIGURED, "reader": {"enabled": False}}
    config = policy.reader(ai_allowance.READER_RECEIPT)
    if not config.enabled:
        return {"suggestions": None, "reason": ai.REASON_DISABLED, "reader": {"enabled": False}}
    if len(data) > config.max_file_bytes:
        return _no_read(ai.REASON_FILE_TOO_LARGE, entity_id, policy)
    context = build_context(entity_id)
    if context is None:
        logger.info("receipt reader: no accounts or suppliers for entity=%s", entity_id)
        return _no_read(ai.REASON_NO_CONTEXT, entity_id, policy)
    read_id, allowance = ai_allowance.reserve(entity_id, user_id, ai_allowance.MODULE_PETTY_CASH, policy)
    if read_id is None:
        logger.info("receipt reader: allowance used up entity=%s", entity_id)
        return {"suggestions": None, "reason": ai.REASON_LIMIT_REACHED,
                "reader": {"enabled": True, **allowance.as_dict()}}
    result = ai.Extraction(reason=ai.REASON_PROVIDER_ERROR)
    try:
        document, document_mime = prepare_for_model(data, mime)
        instruction, prefix, aliases = prompt(context)
        result = ai.read(document, document_mime, config=config, instruction=instruction,
                         prefix_text=prefix, closing_text=ai_prompt.CLOSING, reply_model=Reading,
                         validate=lambda parsed: validate(parsed, context, aliases),
                         models=policy.models,
                         labels=ai.request_labels(entity_id, ai.FEATURE_PETTY_CASH))
    finally:
        # Even if something here raised, the reserved read is closed (failed, so never counted).
        ai_allowance.finish(read_id, result)
    ai.log_read(config, result, entity_id=entity_id, user_id=user_id)
    answer = result.answer()
    # what the file may be instead of a receipt (the user, 2026-11-24): a warning beside whatever
    # fills, refused reads included - never a refusal of its own
    suggestions = answer["suggestions"]
    warning = ai.document_warning(suggestions.pop("document_type", None)) if suggestions else None
    refusal = ai.evidence_refusal(suggestions) if suggestions else None
    if refusal:
        # several receipts in the file, or nothing describes what was paid for (owner, 2026-11-23):
        # nothing is filled and the page says why; the read still counts (a model answered)
        logger.info("receipt reader: refused reason=%s entity=%s", refusal, entity_id)
        answer = {"suggestions": None, "reason": refusal}
    elif answer["suggestions"]:
        # another currency: converted at Frankfurter's rate (owner, 2026-11-19) - after the read,
        # outside the model's failover, so it is asked once
        now = today()
        answer["suggestions"] = fx.exchange_for(
            answer["suggestions"], report_date=report_date or now, today=now, places=2, max_amount=_MAX_AMOUNT,
            fetch=fx.frankfurter_rate,
        )
    return {**answer, "document_warning": warning,
            "reader": {"enabled": True, **ai_allowance.status(entity_id, policy).as_dict()}}


def _no_read(reason: str, entity_id, policy: ai_allowance.Policy) -> dict:
    return {"suggestions": None, "reason": reason,
            "reader": {"enabled": True, **ai_allowance.status(entity_id, policy).as_dict()}}
