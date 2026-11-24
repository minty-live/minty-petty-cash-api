"""Reading a receipt with the model: ``POST /reports/expenses/extract`` (``petty_cash/routes/
expenses.py``) and ``petty_cash/services/receipt_reader.py``, on ``minty_shared.ai`` (2026-11-04:
the reader moved here from Flask's ``services/expense_ai.py``, whose receipt cases these are; the
engine's own cases are minty-shared-py's ``tests/test_ai.py``).

No network: the model client is a stand-in - a test that reached Google would test Google."""

from __future__ import annotations

import json
import re
import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections
from django.utils import timezone
from minty_db.enums import EntityRole
from minty_db.models import AccountInfo, AiGrant, AiPolicy, AiRead, UserEntity, XeroContactSync
from minty_shared import ai, ai_prompt
from minty_shared.django import ai_allowance

from petty_cash.services import receipt_reader
from petty_cash.tests.conftest import WEB, headers, make_entity, make_user, url
from petty_cash.tests.test_expenses import JPEG, PNG, lists  # noqa: F401 - the lists fixture

# Every test runs on the shop with its lists (test_expenses.lists): two suppliers, one account on.
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("lists")]


def _reply(**over) -> str:
    """A well-formed reply to the owner's simple prompt (2026-11-20), naming the shop's lists by
    the short ids sent (``lists``: con_01 = con-0001 ABC Supplies, con_02 = con-0002 Taxi Co,
    acc_01 = acc-0001 400 Advertising; acc-0002 410 Cleaning is switched off and never sent)."""
    body = {
        "supplier_contact_id": "con_01", "supplier_name": "ABC Supplies", "account_id": "acc_01",
        "amount": 120.0, "currency": "HKD", "date": "2026-08-28", "description": "Flyers",
    }
    body.update(over)
    return json.dumps(body)


class FakeUsage:
    prompt_token_count = 5000
    candidates_token_count = 300
    thoughts_token_count = 400
    cached_content_token_count = 0


class FakeResponse:
    """A ``generate_content`` reply: one candidate holding ``text``, ended by ``finish``."""

    def __init__(self, text=None, finish="STOP"):
        self.response_id = "response-1"
        self.usage_metadata = FakeUsage()
        self.prompt_feedback = None
        parts = [SimpleNamespace(text=text, thought=None)] if text is not None else []
        self.candidates = [SimpleNamespace(finish_reason=finish, content=SimpleNamespace(parts=parts))]


class FakeModel:
    """The Vertex AI client's ``models.generate_content``. ``by_model`` answers one model its own
    way (the failover); every other model gets ``answer``."""

    def __init__(self):
        self.answer = FakeResponse(_reply())
        self.by_model = {}
        self.requests = []
        self.models = self

    def generate_content(self, **request):
        self.requests.append(request)
        answer = self.by_model.get(request["model"], self.answer)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _parts(request) -> list:
    """The request's parts in order: the company's lists, the receipt, the closing."""
    (content,) = request["contents"]
    return content.parts


@pytest.fixture
def model(monkeypatch, ai_policy) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(ai, "get_client", lambda timeout_s: fake)
    monkeypatch.setattr(ai.time, "sleep", lambda _s: None)
    return fake


def _policy(**changes):
    """Change the policy as an operator's UPDATE would."""
    AiPolicy.objects.filter(id=1).update(**changes)


#: One read with the stand-in model: 5,000 in + 300 out + 400 thinking.
READ_TOKENS = 5700


def _extract(client, shop, user=None, *, receipt=PNG, name="slip.jpg", files=None):
    form = {}
    if files is not None:
        form["receipt"] = files
    elif receipt is not None:
        form["receipt"] = SimpleUploadedFile(name, receipt, content_type="image/jpeg")
    return client.post(url(shop.entity.id, "/reports/expenses/extract"), form,
                       headers=headers((user or shop.owner).id))


# --- the route ---------------------------------------------------------------------------------


def test_a_receipt_is_read_against_the_company_s_own_lists(client, shop, model):
    resp = _extract(client, shop)

    assert resp.status_code == 200, resp.content
    assert resp["Access-Control-Allow-Origin"] == WEB and resp["Cache-Control"] == "no-store"
    body = resp.json()
    assert body["reason"] is None
    # the first read gave the company its one-time grant (10,000 tokens) and used 5,700 of it
    assert body["reader"] == {"enabled": True, "reads_used": None, "reads_limit": None, "reads_left": None,
                              "percent_left": 43, "blocked": False, "resumes_at": None}
    s = body["suggestions"]
    # no confidence is asked (owner, 2026-11-20): every value is filled and marked to check
    assert s["supplier"] == {"value": "ABC Supplies", "confidence": 0.0, "band": "medium",
                             "applied": True, "contact_id": "con-0001", "read_name": "ABC Supplies"}
    assert s["account"]["account_id"] == "acc-0001" and s["account"]["value"] == "400 Advertising"
    assert s["amount"]["value"] == "120.00" and s["description"]["value"] == "Flyers"
    assert s["document_date"]["value"] == "2026-08-28"
    assert (s["currency"], s["entity_currency"]) == ("HKD", "HKD")
    # the model read the bytes as sent, typed by the bytes (a PNG uploaded as image/jpeg goes as image/png)
    [request] = model.requests
    assert _parts(request)[1].inline_data.mime_type == "image/png"


def test_the_read_is_labelled_for_cloud_billing(client, shop, model, monkeypatch):
    """Google's cost per company: env, feature, entity - nothing per receipt or person (2026-11-24)."""
    monkeypatch.setenv("APP_ENV", "development")
    assert _extract(client, shop).status_code == 200
    [request] = model.requests
    assert request["config"].labels == {"env": "development", "feature": "petty_cash",
                                        "entity_id": str(shop.entity.id).lower()}


def test_the_prefix_carries_only_what_a_line_can_use_in_a_stable_order(client, shop, model):
    _extract(client, shop)

    prefix = _parts(model.requests[0])[0].text
    assert prefix.startswith("## Entity\n")
    assert "Base currency: HKD" in prefix
    assert "acc_01 | 400 | Advertising" in prefix
    assert "Cleaning" not in prefix  # switched off for Petty Cash: never suggested
    # suppliers A to Z, so the cached prefix is byte-stable between reads
    assert prefix.index("con_01 | ABC Supplies") < prefix.index("con_02 | Taxi Co")
    # short ids only: the company's own ids never reach the model
    assert "acc-0001" not in prefix and "con-0001" not in prefix
    assert model.requests[0]["config"].system_instruction == ai_prompt.instruction("receipt")
    assert "acc_01" not in json.dumps(model.requests[0]["config"].response_json_schema)


#: A PNG just over 1 MB - ``ai_policy.max_file_mb`` is at least 1.
BIG_PNG = PNG + b"0" * (1024 * 1024)


@pytest.mark.parametrize("policy, receipt, reason", [
    ({"receipt_reader_on": False}, PNG, "disabled"),
    ({"max_file_mb": 1}, BIG_PNG, "file_too_large"),
], ids=["off", "too-large"])
def test_refusals_past_the_checks_are_a_silent_no_suggestion(client, shop, model, policy, receipt, reason):
    _policy(**policy)

    resp = _extract(client, shop, receipt=receipt)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, reason)
    assert model.requests == [] and not AiRead.objects.exists()  # refused before a read: none used


def test_a_company_with_nothing_to_choose_from_asks_no_question(client, shop, model):
    XeroContactSync.objects.filter(entity_id=shop.entity.id).delete()
    AccountInfo.objects.filter(entity_id=shop.entity.id).delete()

    resp = _extract(client, shop)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, "no_context")
    assert model.requests == [] and body["reader"]["percent_left"] == 100


def test_a_failure_of_the_model_is_a_silent_no_suggestion(client, shop, model):
    model.answer = TimeoutError("deadline exceeded")

    resp = _extract(client, shop)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, "timeout")
    assert body["reader"]["percent_left"] == 100  # a failed read uses none up


def test_one_log_line_with_numbers_and_never_the_values(client, shop, model, caplog):
    caplog.set_level("INFO", logger="minty-shared.ai")

    _extract(client, shop)

    [line] = [r.getMessage() for r in caplog.records if "receipt_ai: extract" in r.getMessage()]
    assert f"entity={shop.entity.id}" in line and "tokens_in=5000" in line and "status=ok" in line
    assert "model=gemini-3.8-flash attempts=gemini-3.8-flash:ok" in line
    # the ids are random: one holding "120" once failed CI (2026-11-11), so they go before the check
    bare = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", line)
    assert "ABC Supplies" not in bare and "120" not in bare and "Flyers" not in bare


@pytest.mark.parametrize("receipt, message", [
    (None, "Attach the receipt to read it."),
    (b"GIF89a-not-allowed", "Attach a PDF, JPEG, PNG or HEIC file."),
    (b"", "That file is empty. Attach the receipt again."),
    (b"%PDF" + b"0" * (10 * 1024 * 1024), "Attach a file of 10 MB or less."),
], ids=["missing", "gif", "empty", "too-big"])
def test_a_file_that_is_not_a_receipt_is_refused_here(client, shop, model, receipt, message):
    resp = _extract(client, shop, receipt=receipt)

    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert model.requests == []


def test_two_files_are_refused_here(client, shop, model):
    files = [SimpleUploadedFile("a.jpg", JPEG), SimpleUploadedFile("b.jpg", JPEG)]

    resp = _extract(client, shop, files=files)

    assert resp.status_code == 422 and resp.json() == {"error": "Attach one receipt at a time."}
    assert model.requests == []


def test_who_may_ask_is_decided_here(client, shop, model):
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    stranger = make_user("stranger@test.com")

    refused = _extract(client, shop, user=base)
    assert refused.status_code == 403
    assert refused.json() == {"error": "You do not have permission to edit reports for this entity."}
    assert _extract(client, shop, user=stranger).status_code == 403  # the door: not a member
    no_token = client.post(url(shop.entity.id, "/reports/expenses/extract"),
                           {"receipt": SimpleUploadedFile("a.jpg", JPEG)})
    assert no_token.status_code == 401
    assert model.requests == []


# --- the reply is untrusted input --------------------------------------------------------------


def _read(client, shop, model, **reply):
    model.answer = FakeResponse(_reply(**reply))
    body = _extract(client, shop).json()
    return body["suggestions"], body["reason"]


def test_a_supplier_id_not_sent_is_dropped_and_the_rest_survives(client, shop, model):
    s, _ = _read(client, shop, model, supplier_contact_id="con-invented", supplier_name="Other Org Ltd")
    assert s["supplier"]["contact_id"] == "" and s["supplier"]["applied"] is False
    assert s["amount"]["applied"] is True


@pytest.mark.parametrize("account_id", ["acc_02", "acc-0001", "acc-0002"],
                         ids=["not-sent", "a-real-id-not-a-short-one", "switched-off"])
def test_an_account_id_not_sent_is_dropped(client, shop, model, account_id):
    s, _ = _read(client, shop, model, account_id=account_id)
    assert s["account"]["account_id"] == "" and s["account"]["applied"] is False


@pytest.mark.parametrize("answered", ["", "con-invented"], ids=["no-id", "an-id-not-sent"])
def test_the_same_name_in_the_list_is_that_supplier_however_it_is_typed(client, shop, model, answered):
    """The user, 2026-11-23: a supplier the list has is chosen, never offered as a new one - the
    name as printed compared as Minty compares names (case, spaces, punctuation ignored)."""
    s, _ = _read(client, shop, model, supplier_contact_id=answered, supplier_name="abc-SUPPLIES")
    assert (s["supplier"]["contact_id"], s["supplier"]["value"]) == ("con-0001", "ABC Supplies")
    assert s["supplier"]["applied"] is True
    assert "detected_name" not in s["supplier"] and "new_supplier_token" not in s["supplier"]


def test_the_name_as_printed_comes_with_the_chosen_supplier(client, shop, model):
    """The user, 2026-11-23: the page shows the receipt's name beside a "similar" pick ("滬江 -> Wu
    Kong") so a wrong one is easy to spot."""
    _policy(default_grant={"tokens": 100000})  # three reads
    s, _ = _read(client, shop, model, supplier_name="  ABC   Supplies (Central) ")
    assert (s["supplier"]["contact_id"], s["supplier"]["read_name"]) == ("con-0001", "ABC Supplies (Central)")
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="Caspita")
    assert s["supplier"]["read_name"] == "Caspita"
    s, _ = _read(client, shop, model, supplier_name=" ")
    assert "read_name" not in s["supplier"]


def test_an_unmatched_supplier_name_is_offered_capped(client, shop, model):
    _policy(default_grant={"tokens": 100000})  # three reads
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="Caspita Restaurant")
    assert (s["supplier"]["applied"], s["supplier"]["detected_name"]) == (False, "Caspita Restaurant")
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="z" * 400)
    assert len(s["supplier"]["detected_name"]) == 100
    s, _ = _read(client, shop, model)
    assert "detected_name" not in s["supplier"] and "new_supplier_token" not in s["supplier"]


def test_an_unmatched_supplier_comes_with_its_name_signed_for_this_company(client, shop, model, settings):
    """The user, 2026-11-23: a missing contact is added automatically when the line is saved; the
    token is the only way the save takes the name (never the page's word)."""
    from minty_shared import ai

    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="  Caspita   Restaurant ")
    token = s["supplier"]["new_supplier_token"]
    assert ai.new_supplier_name(settings.SECRET_KEY, token, shop.entity.id) == "Caspita Restaurant"
    assert ai.new_supplier_name(settings.SECRET_KEY, token, "another-company") is None


def test_no_name_read_means_no_token(client, shop, model):
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="  ")
    assert s["supplier"]["detected_name"] == "" and "new_supplier_token" not in s["supplier"]


@pytest.mark.parametrize("amount", [0, -50, 99_999_999_999])
def test_an_out_of_range_amount_is_dropped(client, shop, model, amount):
    s, _ = _read(client, shop, model, amount=amount)
    assert s["amount"]["value"] == "" and s["amount"]["applied"] is False


def test_the_description_is_capped_and_an_unknown_currency_dropped(client, shop, model):
    s, _ = _read(client, shop, model, description="x" * 5000, currency="dollars")
    assert len(s["description"]["value"]) == 200 and s["currency"] == ""


def test_every_answered_field_is_filled_and_marked_to_check(client, shop, model):
    s, _ = _read(client, shop, model)
    for key in ("supplier", "account", "amount", "description", "document_date"):
        assert (s[key]["band"], s[key]["applied"]) == ("medium", True), key


@pytest.mark.parametrize("value", ["28/08/2026", "2026-02-30", "soon"])
def test_a_date_that_is_not_iso_is_dropped(client, shop, model, value):
    s, _ = _read(client, shop, model, date=value)
    assert s["document_date"]["value"] == "" and s["document_date"]["applied"] is False


#: A reply that found nothing.
EMPTY = {"supplier_contact_id": "", "supplier_name": "", "account_id": "", "amount": 0,
         "currency": "", "date": "", "description": ""}


def test_nothing_usable_is_not_enough_evidence(client, shop, model):
    # owner, 2026-11-23: nothing describes what was paid for, so the page says why
    s, reason = _read(client, shop, model, **EMPTY)
    assert (s, reason) == (None, "no_description")


# --- the owner's launch decisions (2026-11-23, after IRD's "A Guide to Keeping Business Records")


def test_a_receipt_with_no_description_is_not_enough_evidence(client, shop, model):
    s, reason = _read(client, shop, model, description="  ")
    assert (s, reason) == (None, "no_description")


def test_several_receipts_in_one_file_are_refused_before_anything_else(client, shop, model):
    s, reason = _read(client, shop, model, document_count=2, description="")
    assert (s, reason) == (None, "several_documents")


def test_an_inferred_description_is_filled_and_marked_to_check_harder(client, shop, model):
    s, reason = _read(client, shop, model, description="Groceries", description_inferred=True)
    assert reason is None
    assert (s["description"]["value"], s["description"]["applied"]) == ("Groceries", True)
    assert (s["description"]["band"], s["description"]["inferred"]) == ("low", True)
    assert s["account"]["account_id"] == "acc-0001"  # the account is still chosen


def test_a_printed_description_stays_in_the_usual_band(client, shop, model):
    s, _ = _read(client, shop, model, description_inferred=False)
    assert (s["description"]["band"], s["description"]["inferred"]) == ("medium", False)


def test_a_refused_read_still_counts(client, shop, model):
    model.answer = FakeResponse(_reply(document_count=3))

    body = _extract(client, shop).json()

    assert body["reason"] == "several_documents" and body["reader"]["percent_left"] == 43
    [row] = AiRead.objects.filter(entity_id=shop.entity.id)
    assert row.status == "ok"


# --- what the file may be instead (the user, 2026-11-24): a warning, never a refusal -----------


def test_a_bank_payment_confirmation_still_fills_and_carries_the_warning(client, shop, model):
    model.answer = FakeResponse(_reply(document_type="bank_payment_confirmation"))
    body = _extract(client, shop).json()
    assert body["document_warning"] == "bank_payment_confirmation"
    assert body["reason"] is None and body["suggestions"]["amount"]["applied"] is True
    assert "document_type" not in body["suggestions"]


@pytest.mark.parametrize("kind", ["receipt", "invoice", "", "a selfie"])
def test_a_receipt_an_invoice_or_an_unknown_type_warns_of_nothing(client, shop, model, kind):
    model.answer = FakeResponse(_reply(document_type=kind))
    assert _extract(client, shop).json()["document_warning"] is None


def test_the_warning_travels_on_a_refused_read_too(client, shop, model):
    model.answer = FakeResponse(_reply(description="", document_type="other"))
    body = _extract(client, shop).json()
    assert (body["suggestions"], body["reason"], body["document_warning"]) == (
        None, "no_description", "other",
    )


def test_a_read_that_never_answered_has_no_warning(client, shop, model):
    model.answer = FakeResponse("{not json")
    assert _extract(client, shop).json().get("document_warning") is None


def test_the_instruction_is_the_owners_simple_prompt(shop):
    instruction, prefix, aliases = receipt_reader.prompt(receipt_reader.build_context(shop.entity.id))
    assert instruction == ai_prompt.instruction("receipt")
    assert prefix.startswith("## Entity\n")
    assert (aliases.account("acc_01"), aliases.supplier("con_02")) == ("acc-0001", "con-0002")


# --- the connectivity check (Flask's `flask expense-ai check`, moved here) ----------------------


def test_the_check_command_sends_a_generated_image_and_reports_the_round_trip(model, capsys):
    from django.core.management import call_command

    model.answer = FakeResponse(_reply(**EMPTY))
    call_command("expense_ai_check")

    out = capsys.readouterr().out
    assert "models           : gemini-3.8-flash" in out and "Round trip OK" in out
    assert "answered by      : gemini-3.8-flash" in out
    assert "tokens in / out  : 5000 / 300" in out
    [request] = model.requests
    assert _parts(request)[1].inline_data.mime_type == "image/png"
    assert "Connectivity check" in _parts(request)[0].text
    assert "route            : Vertex AI" in out and "receipt reader   : on" in out
    assert "location         : global" in out  # owner, 2026-11-23: always Vertex's global endpoint


def test_the_check_command_fails_loudly_when_the_model_does_not_answer(model):
    from django.core.management import CommandError, call_command

    model.answer = TimeoutError("deadline exceeded")
    with pytest.raises(CommandError, match="FAILED: timeout"):
        call_command("expense_ai_check")


# --- the allowance: each company's grants, shared with Payment Request (owner, 2026-11-18) ------


def test_the_first_read_gives_the_one_time_grant_and_a_used_up_grant_stops_reading(client, shop, model):
    first = _extract(client, shop).json()["reader"]    # 5,700 of 10,000
    second = _extract(client, shop).json()["reader"]   # 11,400: used up (the crossing read finishes)
    refused = _extract(client, shop).json()

    assert (first["percent_left"], first["blocked"]) == (43, False)
    assert (second["percent_left"], second["blocked"]) == (0, True)
    assert (refused["suggestions"], refused["reason"]) == (None, "limit_reached")
    assert len(model.requests) == 2
    [grant] = AiGrant.objects.filter(entity_id=shop.entity.id)
    assert (grant.source, grant.tokens_total, grant.granted_by) == ("default", 10000, "ai_policy")
    assert AiRead.objects.filter(entity_id=shop.entity.id, grant_id=grant.id).count() == 2


def test_the_default_grant_is_given_once_ever(client, shop, model):
    for _ in range(3):
        _extract(client, shop)
    assert AiGrant.objects.filter(entity_id=shop.entity.id).count() == 1


def test_a_top_up_lets_a_used_up_company_read_again_and_is_charged_next(client, shop, model):
    _extract(client, shop)
    _extract(client, shop)
    top_up = ai_allowance.give_grant(shop.entity.id, ai_allowance.GrantSpec(tokens=40000), granted_by="ops")

    body = _extract(client, shop).json()

    assert body["reason"] is None and body["reader"]["percent_left"] == 86  # 34,300 of 40,000
    assert AiRead.objects.filter(grant_id=top_up).count() == 1  # charged to the top-up, not the spent grant


def test_no_default_grant_means_only_grants_given_by_hand(client, shop, model):
    _policy(default_grant=None)

    refused = _extract(client, shop).json()
    assert refused["reason"] == "limit_reached" and refused["reader"]["percent_left"] == 0
    ai_allowance.give_grant(shop.entity.id, ai_allowance.GrantSpec(reads=2), granted_by="ops")
    after = _extract(client, shop).json()["reader"]

    assert (after["reads_left"], after["reads_limit"], after["percent_left"]) == (1, 2, 50)
    assert len(model.requests) == 1


def test_an_expired_grant_no_longer_counts(client, shop, model):
    _policy(default_grant=None)
    grant = ai_allowance.give_grant(shop.entity.id, ai_allowance.GrantSpec(tokens=50000, days=30))
    AiGrant.objects.filter(id=grant).update(expires_at=timezone.now() - timedelta(minutes=1))

    assert _extract(client, shop).json()["reason"] == "limit_reached"
    assert model.requests == []


def test_a_failed_read_uses_none_of_the_grant_but_its_tokens_are_recorded(client, shop, model):
    model.answer = FakeResponse("{not json")

    body = _extract(client, shop).json()

    assert body["reason"] == "invalid_reply" and body["reader"]["percent_left"] == 100
    [row] = AiRead.objects.filter(entity_id=shop.entity.id)
    assert (row.status, row.module, row.reason) == ("failed", "petty_cash", "invalid_reply")
    assert (row.tokens_in, row.tokens_out, row.tokens_thought) == (5000, 300, 400)
    assert row.model_answered is None and row.finished_at is not None


def test_nothing_usable_is_still_a_read(client, shop, model):
    model.answer = FakeResponse(_reply(**EMPTY))

    body = _extract(client, shop).json()

    assert body["reason"] == "no_description" and body["reader"]["percent_left"] == 43


def test_payment_request_reads_use_the_same_grants(client, shop, model):
    grant = ai_allowance.give_grant(shop.entity.id, ai_allowance.GrantSpec(tokens=10000))
    AiRead.objects.create(entity_id=shop.entity.id, grant_id=grant, module="payment_request", status="ok",
                          tokens_in=9000, tokens_out=500)

    body = _extract(client, shop).json()  # 500 left: this read may finish, then nothing
    assert body["reason"] is None and body["reader"]["blocked"] is True
    assert _extract(client, shop).json()["reason"] == "limit_reached"


def test_another_company_s_grants_and_reads_are_its_own(client, shop, model):
    other = make_entity(shop.owner, name="Other Shop", currency=shop.currency)
    used = ai_allowance.give_grant(other.id, ai_allowance.GrantSpec(tokens=100))
    AiRead.objects.create(entity_id=other.id, grant_id=used, module="petty_cash", status="ok", tokens_in=500)

    assert _extract(client, shop).json()["reader"]["percent_left"] == 43


def test_the_backup_answers_when_the_primary_fails_and_the_row_says_so(client, shop, model, caplog):
    caplog.set_level("INFO", logger="minty-shared.ai")
    _policy(primary_model="gemini-3.5-flash-lite", backup_model="gemini-3.8-flash")
    model.by_model["gemini-3.5-flash-lite"] = TimeoutError("deadline exceeded")

    body = _extract(client, shop).json()

    assert body["reason"] is None and body["suggestions"]["amount"]["value"] == "120.00"
    assert [r["model"] for r in model.requests] == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    [row] = AiRead.objects.filter(entity_id=shop.entity.id)
    assert (row.status, row.model_answered) == ("ok", "gemini-3.8-flash")
    assert row.attempts == "gemini-3.5-flash-lite:timeout>gemini-3.8-flash:ok"
    [line] = [r.getMessage() for r in caplog.records if "receipt_ai: extract" in r.getMessage()]
    assert "model=gemini-3.8-flash attempts=gemini-3.5-flash-lite:timeout>gemini-3.8-flash:ok" in line


def test_a_model_change_takes_effect_on_the_next_read(client, shop, model):
    _policy(default_grant={"tokens": 100000})
    _extract(client, shop)
    _policy(primary_model="gemini-3.1-pro-preview")
    _extract(client, shop)

    assert [r["model"] for r in model.requests] == ["gemini-3.8-flash", "gemini-3.1-pro-preview"]


def test_an_extra_daily_cap_pauses_reading_until_the_window_moves(client, shop, model):
    _policy(default_grant={"tokens": 100000}, limit_rules=[{"reads": 1, "period_days": 1}])

    first = _extract(client, shop).json()["reader"]
    paused = _extract(client, shop).json()

    assert first["blocked"] is True and first["percent_left"] == 95  # the grant still has room
    assert paused["reason"] == "limit_reached" and paused["reader"]["resumes_at"] is not None
    assert len(model.requests) == 1


def test_a_read_outside_a_cap_s_window_no_longer_counts(client, shop, model):
    _policy(default_grant={"tokens": 100000}, limit_rules=[{"reads": 1, "period_days": 30}])
    old = AiRead.objects.create(entity_id=shop.entity.id, module="petty_cash", status="ok")
    AiRead.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=31))

    assert _extract(client, shop).json()["reason"] is None


def test_a_read_left_pending_by_a_crash_stops_holding_a_place(client, shop, model):
    _policy(default_grant=None)
    grant = ai_allowance.give_grant(shop.entity.id, ai_allowance.GrantSpec(reads=1))
    stale = AiRead.objects.create(entity_id=shop.entity.id, grant_id=grant, module="payment_request",
                                  status="pending")
    AiRead.objects.filter(id=stale.id).update(created_at=timezone.now() - timedelta(minutes=6))

    assert _extract(client, shop).json()["reason"] is None


def test_no_policy_turns_reading_off_loudly(client, shop, model, caplog):
    AiPolicy.objects.all().delete()

    body = _extract(client, shop).json()

    assert (body["reason"], body["reader"]) == ("not_configured", {"enabled": False})
    assert model.requests == [] and "ai_policy has no row" in caplog.text


@pytest.mark.parametrize("change", [{"limit_rules": [{"reads": "ten"}]}, {"default_grant": {"credits": 5}}])
def test_a_policy_that_does_not_parse_turns_reading_off_loudly(client, shop, model, caplog, change):
    _policy(**change)

    assert _extract(client, shop).json()["reader"] == {"enabled": False}
    assert "ai_policy is unusable" in caplog.text and model.requests == []


@pytest.mark.django_db(transaction=True)
def test_two_reads_with_room_for_one_let_exactly_one_through(shop, ai_policy):
    """The per-company lock: both services reserve under one Postgres advisory lock per company."""
    if connection.vendor != "postgresql":
        pytest.skip("the advisory lock is Postgres's; SQLite serialises writers itself")
    AiPolicy.objects.filter(id=1).update(default_grant={"reads": 1})
    policy = ai_allowance.load_policy()
    results, start = [], threading.Barrier(2)

    def reserve():
        start.wait()
        try:
            results.append(ai_allowance.reserve(shop.entity.id, shop.owner.id, "petty_cash", policy)[0])
        finally:
            connections.close_all()

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r is None for r in results) == [False, True]
    assert AiRead.objects.filter(entity_id=shop.entity.id).count() == 1
    assert AiGrant.objects.filter(entity_id=shop.entity.id).count() == 1  # the default given once


# --- the operator's command ---------------------------------------------------------------------


def test_the_policy_command_changes_models_default_grant_and_caps_after_checking_them(ai_policy, capsys):
    from django.core.management import call_command

    call_command("ai_policy", "--set-models", "gemini-3.5-flash-lite", "gemini-3.8-flash",
                 "--set-default-grant", '{"tokens": 50000, "days": 90}',
                 "--set-rules", '[{"reads": 20, "period_days": 1}]', "--by", "ops")

    ai_policy.refresh_from_db()
    assert (ai_policy.primary_model, ai_policy.backup_model) == ("gemini-3.5-flash-lite", "gemini-3.8-flash")
    assert ai_policy.default_grant == {"tokens": 50000, "days": 90}
    assert ai_policy.limit_rules == [{"reads": 20, "period_days": 1}]
    assert ai_policy.updated_by == "ops"
    out = capsys.readouterr().out
    assert "models        : gemini-3.5-flash-lite -> gemini-3.8-flash" in out
    assert 'default grant : {"tokens": 50000, "days": 90}' in out


def test_the_policy_command_sets_a_price_and_names_a_model_only_with_one(ai_policy, capsys):
    """Each model's price is ai_policy.model_prices (the user, 2026-11-24): a model is named with its price."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="no price for gemini-3.5-flash"):
        call_command("ai_policy", "--set-models", "gemini-3.5-flash")
    call_command("ai_policy", "--set-price", "gemini-3.5-flash", "1.50", "9.00", "0.15",
                 "--set-models", "gemini-3.5-flash")

    ai_policy.refresh_from_db()
    assert ai_policy.primary_model == "gemini-3.5-flash"
    assert ai_policy.model_prices["gemini-3.5-flash"] == {"input": 1.5, "output": 9.0, "cached_input": 0.15}
    assert "gemini-3.8-flash" in ai_policy.model_prices  # the others are kept
    assert "price         : gemini-3.5-flash USD 1.5 in / 9.0 out / 0.15 cached in" in capsys.readouterr().out
    with pytest.raises(CommandError, match="three numbers"):
        call_command("ai_policy", "--set-price", "gemini-3.5-flash", "cheap", "9", "0.15")


def test_a_read_s_cost_comes_from_the_policy_s_price(client, shop, model):
    """5,000 in (none cached) + 300 out + 400 thinking at gemini-3.8-flash's policy price."""
    _policy(model_prices={"gemini-3.8-flash": {"input": 1.0, "output": 10.0, "cached_input": 0.1}})

    assert _extract(client, shop).status_code == 200

    [read] = AiRead.objects.filter(entity_id=shop.entity.id)
    assert read.estimated_cost == Decimal("0.012000")  # (5000 * 1 + 700 * 10) / 1M


def test_the_policy_command_can_turn_the_automatic_grant_off(ai_policy):
    from django.core.management import call_command

    call_command("ai_policy", "--set-default-grant", "null")

    ai_policy.refresh_from_db()
    assert ai_policy.default_grant is None


def test_the_policy_command_gives_a_company_a_grant(ai_policy, shop, capsys):
    from django.core.management import call_command

    call_command("ai_policy", "--grant", str(shop.entity.id), "--tokens", "50000", "--days", "30",
                 "--note", "pilot", "--by", "ops")

    [grant] = AiGrant.objects.filter(entity_id=shop.entity.id)
    assert (grant.tokens_total, grant.reads_total, grant.source) == (50000, None, "manual")
    assert (grant.granted_by, grant.note) == ("ops", "pilot") and grant.expires_at is not None
    out = capsys.readouterr().out
    assert "Grant given to Acme Shop" in out and "0/50000 tokens" in out


@pytest.mark.parametrize("args, says", [
    (["--set-rules", '[{"reads": "ten"}]'], "whole number"),
    (["--set-rules", "not json"], "not JSON"),
    (["--set-default-grant", '{"days": 30}'], "gives nothing"),
    (["--set-models", "openai:gpt-6.1-terra"], "approved provider"),
    (["--set-models", "gemini-3.8-flash", "gemini-3.8-flash"], "different model"),
    (["--timeout-s", "200"], "at most 120"),
    (["--max-output-tokens", "100"], "max_output_tokens"),
    (["--thinking-level", "extreme"], "thinking_level"),
    (["--max-file-mb", "50"], "at most 20"),
    # owner, 2026-11-23: no region setting - every read is on Vertex's global endpoint
    (["--location", "global"], "unrecognized arguments"),
])
def test_the_policy_command_refuses_a_change_that_would_break_reading(ai_policy, args, says):
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match=says):
        call_command("ai_policy", *args)
    ai_policy.refresh_from_db()
    assert (ai_policy.primary_model, ai_policy.default_grant) == ("gemini-3.8-flash", {"tokens": 10000})
    assert (ai_policy.timeout_s, ai_policy.max_output_tokens, ai_policy.thinking_level) == (25, 4096, "low")


def test_the_policy_command_sets_the_readers_switches_and_limits(ai_policy, capsys):
    """Owner, 2026-11-20: the readers' settings are the row's, changed by the command for both
    services at once; what is not named keeps its value."""
    from django.core.management import call_command

    call_command("ai_policy", "--receipt-reader", "off", "--invoice-reader", "on", "--timeout-s", "40",
                 "--max-output-tokens", "6000", "--thinking-level", "medium", "--max-file-mb", "8",
                 "--by", "ops")

    ai_policy.refresh_from_db()
    assert (ai_policy.receipt_reader_on, ai_policy.invoice_reader_on) == (False, True)
    assert (ai_policy.timeout_s, ai_policy.max_output_tokens) == (40, 6000)
    assert (ai_policy.thinking_level, ai_policy.max_file_mb, ai_policy.updated_by) == ("medium", 8, "ops")
    assert ai_policy.primary_model == "gemini-3.8-flash" and ai_policy.default_grant == {"tokens": 10000}
    out = capsys.readouterr().out
    assert "readers       : receipt off, invoice on" in out
    assert "limits        : timeout 40 s, 6000 output tokens, thinking medium, files up to 8 MB" in out
    assert "region        : global (Vertex AI)" in out


def test_the_policy_command_refuses_a_grant_of_nothing(ai_policy, shop):
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match="gives nothing"):
        call_command("ai_policy", "--grant", str(shop.entity.id), "--days", "30")
    assert not AiGrant.objects.exists()
